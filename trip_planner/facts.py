"""Provider-neutral fact and evidence contracts.

This module is deliberately pure.  It does not call providers, read or write
cache files, mutate canonical plans, or decide whether arbitrary research text
is trustworthy.  Trusted adapters may create only the small normalized value
schemas below; external responses and prose never enter the planning kernel.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence, TypeAlias
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import EvidenceState


FACT_QUERY_VERSION = "fact-query/v1"
FACT_OBSERVATION_VERSION = "fact-observation/v1"
EVIDENCE_SNAPSHOT_VERSION = "evidence-snapshot/v1"
PROVIDER_RESULT_VERSION = "provider-result/v1"
GOOGLE_MAPS_NON_EEA_POLICY_PROFILE = (
    "google-maps-non-eea-2026-06-10"
)
_GOOGLE_ROUTE_FIELD_MASK = (
    "routes.distanceMeters,"
    "routes.duration,"
    "routes.staticDuration,"
    "routes.warnings,"
    "fallbackInfo.routingMode,"
    "fallbackInfo.reason"
)

Scalar: TypeAlias = str | int | float | bool | None

_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_NAME_RE = re.compile(r"[a-z][a-z0-9._/-]{0,127}")
_QUALIFIER_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_RFC3339_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T"
    r"\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?"
    r"(?:Z|[+-]\d{2}:\d{2})"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|credential|password|secret|token)"
    r"\s*['\"]?\s*(?:[:=]|/)\s*['\"]?\s*[^\s'\"/?#&]+"
    r"|(?:bearer|basic)\s+[a-z0-9._~+/=-]{8,}"
)
_SENSITIVE_QUALIFIER_PARTS = frozenset(
    {
        "api",
        "authorization",
        "credential",
        "key",
        "password",
        "signature",
        "secret",
        "session",
        "sig",
        "token",
    }
)
_TRAVEL_MODES = frozenset(
    {"driving", "walking", "transit", "bicycling", "two_wheeler"}
)
_BUSINESS_STATUSES = frozenset(
    {
        "operational",
        "closed_temporarily",
        "closed_permanently",
        "future_opening",
    }
)
_MAX_SUBJECTS = 16
_MAX_QUALIFIERS = 32
_MAX_PAYLOAD_BYTES = 32_768
_MAX_INTERVALS = 128
_MAX_WARNINGS = 16
_MAX_ATTRIBUTIONS = 32
_MAX_RESULT_ITEMS = 256
_MAX_GENERATION = 2**63 - 1


class FactContractError(ValueError):
    """A malformed or unsafe fact/provider contract value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FactKind(str, Enum):
    """Small V1 claim vocabulary; not a general fact ontology."""

    PLACE_IDENTITY = "place_identity"
    PLACE_PROFILE = "place_profile"
    PLACE_OPENING_HOURS = "place_opening_hours"
    ROUTE_ESTIMATE = "route_estimate"
    FLIGHT_OFFER = "flight_offer"
    HOTEL_OFFER = "hotel_offer"


class ProviderResultStatus(str, Enum):
    """Whether a provider request returned usable normalized observations."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CACHE_HIT = "cache_hit"


class EvidencePersistence(str, Enum):
    """Whether an authorized normalized value may cross a process boundary."""

    MEMORY_ONLY = "memory_only"
    DISK_TTL = "disk_ttl"
    INDEFINITE_ID = "indefinite_id"


class ProviderProblemCode(str, Enum):
    """Stable failures that never carry raw provider exceptions."""

    INVALID_PROVIDER_REQUEST = "INVALID_PROVIDER_REQUEST"
    INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"
    UNSUPPORTED_FACT_KIND = "UNSUPPORTED_FACT_KIND"
    UNTRUSTED_PROVENANCE = "UNTRUSTED_PROVENANCE"
    EVIDENCE_BINDING_MISMATCH = "EVIDENCE_BINDING_MISMATCH"
    PROVIDER_BUDGET_EXHAUSTED = "PROVIDER_BUDGET_EXHAUSTED"
    PROVIDER_CALL_RESERVED = "PROVIDER_CALL_RESERVED"
    PENDING_REVIEW = "PENDING_REVIEW"
    EVIDENCE_REVISION_CHANGED = "EVIDENCE_REVISION_CHANGED"
    OUTSIDE_PROVIDER_HORIZON = "OUTSIDE_PROVIDER_HORIZON"
    TRANSIT_UNAVAILABLE = "TRANSIT_UNAVAILABLE"
    AUTH_FAILED = "AUTH_FAILED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED_MODE = "UNSUPPORTED_MODE"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    OUT_OF_SCOPE_RESULT = "OUT_OF_SCOPE_RESULT"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    CONFLICT_DETECTED = "CONFLICT_DETECTED"
    RETENTION_EXPIRED = "RETENTION_EXPIRED"
    STALE_PROVIDER_RESULT = "STALE_PROVIDER_RESULT"
    CACHE_CORRUPTED = "CACHE_CORRUPTED"
    CACHE_WRITE_FAILED = "CACHE_WRITE_FAILED"
    CACHE_OUTCOME_UNKNOWN = "CACHE_OUTCOME_UNKNOWN"


_AUTOMATIC_RETRY_CODES = frozenset(
    {
        ProviderProblemCode.RATE_LIMITED,
        ProviderProblemCode.TIMEOUT,
        ProviderProblemCode.PROVIDER_UNAVAILABLE,
    }
)
_OUTBOUND_FAILURE_CODES = frozenset(
    {
        ProviderProblemCode.AUTH_FAILED,
        ProviderProblemCode.QUOTA_EXHAUSTED,
        ProviderProblemCode.RATE_LIMITED,
        ProviderProblemCode.TIMEOUT,
        ProviderProblemCode.PROVIDER_UNAVAILABLE,
        ProviderProblemCode.EMPTY_RESPONSE,
        ProviderProblemCode.NOT_FOUND,
        ProviderProblemCode.AMBIGUOUS_MATCH,
        ProviderProblemCode.OUT_OF_SCOPE_RESULT,
        ProviderProblemCode.PARTIAL_FAILURE,
    }
)


class ResolutionReason(str, Enum):
    """Why one exact fact is or is not usable."""

    FRESH = "FRESH_EVIDENCE"
    STALE = "STALE_EVIDENCE"
    CONFLICTED = "CONFLICT_DETECTED"
    MISSING = "MISSING_EVIDENCE"


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    """One host-owned, static provider/storage authorization profile."""

    policy_id: str
    provider_id: str
    adapter_id: str
    adapter_version: str
    contract_region: str
    allowed_fact_kinds: tuple[FactKind, ...]
    allowed_value_fields: tuple[str, ...]
    allowed_operations: tuple[str, ...]
    persistence: EvidencePersistence
    max_validity_seconds: int
    max_retention_seconds: int | None
    allowed_query_fields: tuple[str, ...] = ()
    required_attribution_labels: tuple[str, ...] = ()
    policy_digest: str = ""

    def __post_init__(self) -> None:
        for value, name in (
            (self.policy_id, "ProviderPolicy.policy_id"),
            (self.provider_id, "ProviderPolicy.provider_id"),
            (self.adapter_id, "ProviderPolicy.adapter_id"),
            (self.adapter_version, "ProviderPolicy.adapter_version"),
            (self.contract_region, "ProviderPolicy.contract_region"),
        ):
            _require_machine_name(value, name)
        if (
            not isinstance(self.allowed_fact_kinds, tuple)
            or not self.allowed_fact_kinds
            or any(
                not isinstance(item, FactKind)
                for item in self.allowed_fact_kinds
            )
            or len(set(self.allowed_fact_kinds))
            != len(self.allowed_fact_kinds)
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "ProviderPolicy.allowed_fact_kinds must be unique FactKind values.",
            )
        normalized_kinds = tuple(
            sorted(self.allowed_fact_kinds, key=lambda item: item.value)
        )
        object.__setattr__(self, "allowed_fact_kinds", normalized_kinds)
        for values, name, allow_empty in (
            (
                self.allowed_value_fields,
                "allowed_value_fields",
                False,
            ),
            (
                self.allowed_query_fields,
                "allowed_query_fields",
                True,
            ),
        ):
            if (
                not isinstance(values, tuple)
                or (not values and not allow_empty)
            ):
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    (
                        f"ProviderPolicy.{name} must be "
                        f"{'a' if allow_empty else 'a non-empty'} tuple."
                    ),
                )
            for value in values:
                if not isinstance(value, str) or not _QUALIFIER_RE.fullmatch(
                    value
                ):
                    raise FactContractError(
                        "INVALID_PROVIDER_REQUEST",
                        f"ProviderPolicy.{name} contains an invalid field name.",
                    )
            if len(set(values)) != len(values):
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    f"ProviderPolicy.{name} cannot contain duplicates.",
                )
            object.__setattr__(self, name, tuple(sorted(values)))
        if (
            not isinstance(self.allowed_operations, tuple)
            or not self.allowed_operations
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "ProviderPolicy.allowed_operations must be a non-empty tuple.",
            )
        for operation in self.allowed_operations:
            _require_machine_name(
                operation, "ProviderPolicy.allowed_operations item"
            )
        if len(set(self.allowed_operations)) != len(
            self.allowed_operations
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "ProviderPolicy.allowed_operations cannot contain duplicates.",
            )
        object.__setattr__(
            self,
            "allowed_operations",
            tuple(sorted(self.allowed_operations)),
        )
        if not isinstance(self.persistence, EvidencePersistence):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "ProviderPolicy.persistence must be EvidencePersistence.",
            )
        if (
            isinstance(self.max_validity_seconds, bool)
            or not isinstance(self.max_validity_seconds, int)
            or self.max_validity_seconds <= 0
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "ProviderPolicy.max_validity_seconds must be positive.",
            )
        if self.persistence is EvidencePersistence.INDEFINITE_ID:
            if (
                self.max_retention_seconds is not None
                or normalized_kinds != (FactKind.PLACE_IDENTITY,)
                or set(self.allowed_value_fields)
                != {"provider_place_id"}
            ):
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    (
                        "Indefinite policies are restricted to provider place "
                        "identity values."
                    ),
                )
        elif (
            isinstance(self.max_retention_seconds, bool)
            or not isinstance(self.max_retention_seconds, int)
            or self.max_retention_seconds <= 0
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "TTL and memory policies require positive max retention.",
            )
        if not isinstance(self.required_attribution_labels, tuple):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "ProviderPolicy.required_attribution_labels must be a tuple.",
            )
        normalized_labels: list[str] = []
        for label in self.required_attribution_labels:
            normalized = _normalized_text(
                label,
                "required attribution label",
                maximum=256,
            )
            _reject_secret_text(
                normalized,
                "INVALID_PROVIDER_REQUEST",
                "required attribution label",
            )
            normalized_labels.append(normalized)
        if len(set(normalized_labels)) != len(normalized_labels):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Required attribution labels cannot contain duplicates.",
            )
        object.__setattr__(
            self,
            "required_attribution_labels",
            tuple(sorted(normalized_labels)),
        )
        expected_digest = _digest(
            {
                "policy_id": self.policy_id,
                "provider_id": self.provider_id,
                "adapter_id": self.adapter_id,
                "adapter_version": self.adapter_version,
                "contract_region": self.contract_region,
                "allowed_fact_kinds": [
                    item.value for item in normalized_kinds
                ],
                "allowed_value_fields": list(self.allowed_value_fields),
                "allowed_operations": list(self.allowed_operations),
                "persistence": self.persistence.value,
                "max_validity_seconds": self.max_validity_seconds,
                "max_retention_seconds": self.max_retention_seconds,
                "allowed_query_fields": list(self.allowed_query_fields),
                "required_attribution_labels": list(
                    self.required_attribution_labels
                ),
            },
            prefix="provider-policy",
        )
        if self.policy_digest and self.policy_digest != expected_digest:
            raise FactContractError(
                "DIGEST_MISMATCH",
                "ProviderPolicy.policy_digest does not match policy content.",
            )
        object.__setattr__(self, "policy_digest", expected_digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "contract_region": self.contract_region,
            "allowed_fact_kinds": [
                item.value for item in self.allowed_fact_kinds
            ],
            "allowed_value_fields": list(self.allowed_value_fields),
            "allowed_operations": list(self.allowed_operations),
            "persistence": self.persistence.value,
            "max_validity_seconds": self.max_validity_seconds,
            "max_retention_seconds": self.max_retention_seconds,
            "allowed_query_fields": list(self.allowed_query_fields),
            "required_attribution_labels": list(
                self.required_attribution_labels
            ),
            "policy_digest": self.policy_digest,
        }


@dataclass(frozen=True, slots=True)
class ProviderPolicyRegistry:
    """Immutable host allowlist; unknown policy IDs fail closed."""

    policies: tuple[ProviderPolicy, ...]
    revision: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.policies, tuple)
            or not self.policies
            or any(type(item) is not ProviderPolicy for item in self.policies)
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "ProviderPolicyRegistry requires exact policies.",
            )
        policy_ids = [item.policy_id for item in self.policies]
        if len(set(policy_ids)) != len(policy_ids):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Provider policy IDs must be globally unique.",
            )
        source_kind_slots = [
            (
                policy.provider_id,
                policy.adapter_id,
                policy.adapter_version,
                kind,
            )
            for policy in self.policies
            for kind in policy.allowed_fact_kinds
        ]
        if len(set(source_kind_slots)) != len(source_kind_slots):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                (
                    "Provider policies cannot overlap the same exact "
                    "provider/adapter/version/fact-kind authorization slot."
                ),
            )
        normalized = tuple(
            sorted(self.policies, key=lambda item: item.policy_id)
        )
        object.__setattr__(self, "policies", normalized)
        expected_revision = _digest(
            [item.to_dict() for item in normalized],
            prefix="provider-policy-registry",
        )
        if self.revision and self.revision != expected_revision:
            raise FactContractError(
                "DIGEST_MISMATCH",
                "ProviderPolicyRegistry.revision does not match policies.",
            )
        object.__setattr__(self, "revision", expected_revision)

    def policy(self, policy_id: str) -> ProviderPolicy:
        for policy in self.policies:
            if policy.policy_id == policy_id:
                return policy
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            f"Provider policy {policy_id!r} is not host-allowlisted.",
        )

    def validate_observation(
        self,
        observation: "FactObservation",
    ) -> ProviderPolicy:
        if type(observation) is not FactObservation:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Policy validation requires an exact FactObservation.",
            )
        policy = self.policy(observation.provenance.retention_policy_id)
        provenance = observation.provenance
        source = (
            provenance.provider_id,
            provenance.adapter_id,
            provenance.adapter_version,
        )
        expected_source = (
            policy.provider_id,
            policy.adapter_id,
            policy.adapter_version,
        )
        if source != expected_source or observation.key.kind not in (
            policy.allowed_fact_kinds
        ):
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Observation source or fact kind is outside provider policy.",
            )
        if not set(observation.value.payload).issubset(
            policy.allowed_value_fields
        ):
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Observation value fields exceed provider storage policy.",
            )
        validity_seconds = (
            observation.valid_until - observation.retrieved_at
        ).total_seconds()
        if validity_seconds > policy.max_validity_seconds:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Observation validity exceeds its host policy.",
            )
        if policy.persistence is EvidencePersistence.INDEFINITE_ID:
            if observation.purge_at is not None:
                raise FactContractError(
                    "UNTRUSTED_PROVENANCE",
                    "Indefinite identity policy requires no purge deadline.",
                )
            if (
                provenance.response_id is not None
                or provenance.source_uri is not None
                or provenance.provider_record_id
                not in {None, observation.value.payload["provider_place_id"]}
                or provenance.attributions
                != tuple(
                    (label, None)
                    for label in policy.required_attribution_labels
                )
            ):
                raise FactContractError(
                    "UNTRUSTED_PROVENANCE",
                    (
                        "Indefinite identity retention permits only the place "
                        "ID and static host-owned attribution requirements."
                    ),
                )
        else:
            if observation.purge_at is None:
                raise FactContractError(
                    "UNTRUSTED_PROVENANCE",
                    "Non-persistent provider content requires purge_at.",
                )
            retention_seconds = (
                observation.purge_at - observation.retrieved_at
            ).total_seconds()
            if (
                policy.max_retention_seconds is None
                or retention_seconds > policy.max_retention_seconds
            ):
                raise FactContractError(
                    "UNTRUSTED_PROVENANCE",
                    "Observation retention exceeds its host policy.",
                )
        labels = {
            label for label, _uri in provenance.attributions
        }
        if not set(policy.required_attribution_labels).issubset(labels):
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Observation is missing provider-required attribution.",
            )
        return policy

    def persistence_for(
        self,
        observation: "FactObservation",
    ) -> EvidencePersistence:
        return self.validate_observation(observation).persistence


def google_maps_policy_registry(
    contract_region: str,
) -> ProviderPolicyRegistry:
    """Return the only built-in Google policy profile; unknown regions reject."""

    if contract_region != GOOGLE_MAPS_NON_EEA_POLICY_PROFILE:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            (
                "Google Maps policy region is unknown or unsupported; "
                "provider content must remain unavailable until host review."
            ),
        )
    day = 24 * 60 * 60
    return ProviderPolicyRegistry(
        policies=(
            ProviderPolicy(
                policy_id="google-place-id-v1",
                provider_id="google-places",
                adapter_id="google-places",
                adapter_version="v1",
                contract_region=contract_region,
                allowed_fact_kinds=(FactKind.PLACE_IDENTITY,),
                allowed_value_fields=("provider_place_id",),
                allowed_operations=("refresh-place-id", "resolve-place"),
                persistence=EvidencePersistence.INDEFINITE_ID,
                max_validity_seconds=366 * day,
                max_retention_seconds=None,
                allowed_query_fields=(
                    "basis_observation_id",
                    "basis_provider_place_id",
                    "basis_snapshot_id",
                    "basis_value_digest",
                    "expected_locality",
                    "expected_name",
                    "expected_primary_types",
                    "field_mask",
                    "language_code",
                    "latitude",
                    "longitude",
                    "page_size",
                    "provider_place_id",
                    "radius_m",
                    "region_code",
                    "text_query",
                ),
                required_attribution_labels=("Google Maps",),
            ),
            ProviderPolicy(
                policy_id="google-place-profile-runtime-v1",
                provider_id="google-places",
                adapter_id="google-places",
                adapter_version="v1",
                contract_region=contract_region,
                allowed_fact_kinds=(FactKind.PLACE_PROFILE,),
                allowed_value_fields=(
                    "business_status",
                    "display_name",
                    "latitude",
                    "longitude",
                    "provider_place_id",
                    "timezone",
                ),
                allowed_operations=("fetch-place-profile",),
                persistence=EvidencePersistence.MEMORY_ONLY,
                max_validity_seconds=30 * day,
                max_retention_seconds=day,
                allowed_query_fields=("language_code", "region_code"),
                required_attribution_labels=("Google Maps",),
            ),
            ProviderPolicy(
                policy_id="google-place-hours-runtime-v1",
                provider_id="google-places",
                adapter_id="google-places",
                adapter_version="v1",
                contract_region=contract_region,
                allowed_fact_kinds=(
                    FactKind.PLACE_OPENING_HOURS,
                ),
                allowed_value_fields=(
                    "basis",
                    "closed_dates",
                    "coverage_end",
                    "coverage_start",
                    "intervals",
                    "provider_place_id",
                    "timezone",
                ),
                allowed_operations=("fetch-opening-hours",),
                persistence=EvidencePersistence.MEMORY_ONLY,
                max_validity_seconds=30 * day,
                max_retention_seconds=day,
                allowed_query_fields=("language_code", "region_code"),
                required_attribution_labels=("Google Maps",),
            ),
            ProviderPolicy(
                policy_id="google-route-runtime-v1",
                provider_id="google-routes",
                adapter_id="google-routes",
                adapter_version="v1",
                contract_region=contract_region,
                allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
                allowed_value_fields=(
                    "arrival_at",
                    "departure_at",
                    "distance_km",
                    "duration_min",
                    "fallback_from_mode",
                    "mode",
                    "static_duration_min",
                    "warning_codes",
                ),
                allowed_operations=("compute-route",),
                persistence=EvidencePersistence.MEMORY_ONLY,
                max_validity_seconds=30 * day,
                max_retention_seconds=day,
                allowed_query_fields=(
                    "basis_evidence_revision",
                    "basis_snapshot_id",
                    "basis_store_revision",
                    "destination_endpoint_id",
                    "destination_observation_id",
                    "destination_value_digest",
                    "fallback_from_mode",
                    "field_mask",
                    "language_code",
                    "origin_endpoint_id",
                    "origin_observation_id",
                    "origin_value_digest",
                    "region_code",
                    "units",
                ),
                required_attribution_labels=("Google Maps",),
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class FactKey:
    """The exact semantic scope of one normalized claim."""

    kind: FactKind
    subject_ids: tuple[str, ...]
    qualifiers: tuple[tuple[str, Scalar], ...] = ()
    contract_version: str = FACT_QUERY_VERSION
    key_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FactKind):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST", "FactKey.kind must be FactKind."
            )
        if self.contract_version != FACT_QUERY_VERSION:
            raise FactContractError(
                "UNSUPPORTED_VERSION",
                f"Unsupported fact query version {self.contract_version!r}.",
            )
        if not isinstance(self.subject_ids, tuple) or not self.subject_ids:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "FactKey.subject_ids must be a non-empty tuple.",
            )
        if len(self.subject_ids) > _MAX_SUBJECTS:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                f"FactKey.subject_ids cannot exceed {_MAX_SUBJECTS} items.",
            )
        normalized_subject_ids: list[str] = []
        for subject_id in self.subject_ids:
            try:
                normalized_subject = _normalized_text(
                    subject_id,
                    "FactKey.subject_ids item",
                    maximum=256,
                )
                _reject_secret_text(
                    normalized_subject,
                    "SECRET_IN_PROVIDER_REQUEST",
                    "FactKey subject",
                )
                normalized_subject_ids.append(normalized_subject)
            except FactContractError as exc:
                if exc.code == "SECRET_IN_PROVIDER_REQUEST":
                    raise
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST", exc.message
                ) from exc
        normalized_subjects = tuple(normalized_subject_ids)
        object.__setattr__(self, "subject_ids", normalized_subjects)
        if len(set(normalized_subjects)) != len(normalized_subjects):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "FactKey.subject_ids cannot contain duplicates.",
            )
        if not isinstance(self.qualifiers, tuple):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "FactKey.qualifiers must be a tuple.",
            )
        if len(self.qualifiers) > _MAX_QUALIFIERS:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                f"FactKey.qualifiers cannot exceed {_MAX_QUALIFIERS} items.",
            )
        normalized: list[tuple[str, Scalar]] = []
        seen: set[str] = set()
        for item in self.qualifiers:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
            ):
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    "FactKey.qualifiers items must be (name, scalar) tuples.",
                )
            name, value = item
            if not _QUALIFIER_RE.fullmatch(name):
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    f"Invalid FactKey qualifier name {name!r}.",
                )
            if _is_sensitive_qualifier(name):
                raise FactContractError(
                    "SECRET_IN_PROVIDER_REQUEST",
                    f"Sensitive qualifier {name!r} cannot enter a fact key.",
                )
            if name in seen:
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    f"Duplicate FactKey qualifier {name!r}.",
                )
            seen.add(name)
            normalized.append(
                (
                    name,
                    _normalize_qualifier(self.kind, name, value),
                )
            )
        normalized_tuple = tuple(sorted(normalized))
        object.__setattr__(self, "qualifiers", normalized_tuple)
        _validate_fact_key_scope(
            self.kind,
            normalized_subjects,
            dict(normalized_tuple),
        )
        expected_key_id = _digest(
            {
                "contract_version": self.contract_version,
                "kind": self.kind.value,
                "subject_ids": list(normalized_subjects),
                "qualifiers": [
                    [name, value] for name, value in normalized_tuple
                ],
            },
            prefix="fact-key",
        )
        if self.key_id and self.key_id != expected_key_id:
            raise FactContractError(
                "DIGEST_MISMATCH",
                "FactKey.key_id does not match its semantic scope.",
            )
        object.__setattr__(self, "key_id", expected_key_id)

    @property
    def qualifier_map(self) -> Mapping[str, Scalar]:
        return dict(self.qualifiers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "kind": self.kind.value,
            "subject_ids": list(self.subject_ids),
            "qualifiers": {
                name: value for name, value in self.qualifiers
            },
            "key_id": self.key_id,
        }


_VALUE_SCHEMAS = {
    FactKind.PLACE_IDENTITY: "place-identity/v1",
    FactKind.PLACE_PROFILE: "place-profile/v1",
    FactKind.PLACE_OPENING_HOURS: "place-opening-hours/v1",
    FactKind.ROUTE_ESTIMATE: "route-estimate/v1",
}


@dataclass(frozen=True, slots=True)
class FactValue:
    """Canonical bytes for one allowlisted, kind-specific value schema."""

    kind: FactKind
    schema_version: str
    canonical_json: bytes = field(repr=False)
    value_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FactKind):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE", "FactValue.kind must be FactKind."
            )
        expected_schema = _VALUE_SCHEMAS.get(self.kind)
        if expected_schema is None:
            raise FactContractError(
                "UNSUPPORTED_FACT_KIND",
                (
                    f"Normalized {self.kind.value!r} values are reserved for "
                    "a later provider slice."
                ),
            )
        if self.schema_version != expected_schema:
            raise FactContractError(
                "UNSUPPORTED_FACT_KIND",
                (
                    f"{self.kind.value!r} requires value schema "
                    f"{expected_schema!r}."
                ),
            )
        if not isinstance(self.canonical_json, bytes):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "FactValue.canonical_json must be bytes.",
            )
        if len(self.canonical_json) > _MAX_PAYLOAD_BYTES:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                f"Normalized fact payload exceeds {_MAX_PAYLOAD_BYTES} bytes.",
            )
        payload = _decode_json_object(self.canonical_json)
        normalized = _normalize_payload(self.kind, payload)
        canonical = _canonical_json(normalized)
        if self.canonical_json != canonical:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "FactValue.canonical_json is not in canonical normalized form.",
            )
        expected_digest = _digest(
            {
                "kind": self.kind.value,
                "schema_version": self.schema_version,
                "payload": normalized,
            },
            prefix="fact-value",
        )
        if self.value_digest and self.value_digest != expected_digest:
            raise FactContractError(
                "DIGEST_MISMATCH",
                "FactValue.value_digest does not match normalized content.",
            )
        object.__setattr__(self, "value_digest", expected_digest)

    @classmethod
    def from_payload(
        cls,
        kind: FactKind,
        payload: Mapping[str, Any],
    ) -> "FactValue":
        if not isinstance(kind, FactKind):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE", "kind must be FactKind."
            )
        schema_version = _VALUE_SCHEMAS.get(kind)
        if schema_version is None:
            raise FactContractError(
                "UNSUPPORTED_FACT_KIND",
                (
                    f"Normalized {kind.value!r} values are reserved for a "
                    "later provider slice."
                ),
            )
        normalized = _normalize_payload(kind, payload)
        return cls(
            kind=kind,
            schema_version=schema_version,
            canonical_json=_canonical_json(normalized),
        )

    @property
    def payload(self) -> dict[str, Any]:
        """Return a detached JSON object."""

        return _decode_json_object(self.canonical_json)

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "schema_version": self.schema_version,
            "payload": self.payload,
            "value_digest": self.value_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted value binding; payload remains runtime-only."""

        return {
            "kind": self.kind.value,
            "schema_version": self.schema_version,
            "value_digest": self.value_digest,
        }


@dataclass(frozen=True, slots=True)
class ProviderProvenance:
    """Secret-free provenance stamped by one trusted adapter."""

    provider_id: str
    adapter_id: str
    adapter_version: str
    request_fingerprint: str
    retention_policy_id: str
    provider_record_id: str | None = field(default=None, repr=False)
    response_id: str | None = field(default=None, repr=False)
    source_uri: str | None = field(default=None, repr=False)
    attributions: tuple[tuple[str, str | None], ...] = field(
        default=(),
        repr=False,
    )

    def __post_init__(self) -> None:
        for value, name in (
            (self.provider_id, "ProviderProvenance.provider_id"),
            (self.adapter_id, "ProviderProvenance.adapter_id"),
            (
                self.adapter_version,
                "ProviderProvenance.adapter_version",
            ),
            (
                self.retention_policy_id,
                "ProviderProvenance.retention_policy_id",
            ),
        ):
            _require_machine_name(value, name)
        _require_digest(
            self.request_fingerprint,
            "ProviderProvenance.request_fingerprint",
        )
        for value, name in (
            (
                self.provider_record_id,
                "ProviderProvenance.provider_record_id",
            ),
            (self.response_id, "ProviderProvenance.response_id"),
        ):
            if value is not None:
                normalized_value = _normalized_text(
                    value, name, maximum=512
                )
                _reject_secret_text(
                    normalized_value,
                    "INVALID_PROVIDER_RESPONSE",
                    name,
                )
                object.__setattr__(
                    self,
                    name.rsplit(".", 1)[-1],
                    normalized_value,
                )
        if self.source_uri is not None:
            normalized_source_uri = _normalized_public_uri(
                self.source_uri,
                "ProviderProvenance.source_uri",
            )
            object.__setattr__(
                self, "source_uri", normalized_source_uri
            )
        if not isinstance(self.attributions, tuple):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "ProviderProvenance.attributions must be a tuple.",
            )
        if len(self.attributions) > _MAX_ATTRIBUTIONS:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                (
                    "ProviderProvenance.attributions cannot exceed "
                    f"{_MAX_ATTRIBUTIONS} items."
                ),
            )
        normalized: list[tuple[str, str | None]] = []
        seen: set[tuple[str, str | None]] = set()
        for item in self.attributions:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or (item[1] is not None and not isinstance(item[1], str))
            ):
                raise FactContractError(
                    "INVALID_PROVIDER_RESPONSE",
                    "Attributions must be (label, optional_uri) tuples.",
                )
            label, uri = item
            label = _normalized_text(
                label, "attribution label", maximum=256
            )
            _reject_secret_text(
                label,
                "INVALID_PROVIDER_RESPONSE",
                "attribution label",
            )
            if uri is not None:
                uri = _normalized_public_uri(uri, "attribution URI")
            normalized_item = (label, uri)
            if normalized_item in seen:
                continue
            seen.add(normalized_item)
            normalized.append(normalized_item)
        object.__setattr__(
            self,
            "attributions",
            tuple(sorted(normalized, key=lambda item: (item[0], item[1] or ""))),
        )

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "request_fingerprint": self.request_fingerprint,
            "retention_policy_id": self.retention_policy_id,
            "provider_record_id": self.provider_record_id,
            "response_id": self.response_id,
            "source_uri": self.source_uri,
            "attributions": [
                {"label": label, "uri": uri}
                for label, uri in self.attributions
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        """Return redacted provenance suitable for durable diagnostics."""

        return {
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "request_fingerprint": self.request_fingerprint,
            "retention_policy_id": self.retention_policy_id,
        }


def provider_request_fingerprint(
    *,
    provider_id: str,
    adapter_id: str,
    adapter_version: str,
    operation: str,
    fact_keys: Iterable[FactKey],
    policy_id: str,
    policy_digest: str,
    query_scope: Iterable[tuple[str, Scalar]] = (),
) -> str:
    """Return an exact, order-independent, secret-free request fingerprint."""

    for value, name in (
        (provider_id, "provider_id"),
        (adapter_id, "adapter_id"),
        (adapter_version, "adapter_version"),
        (operation, "operation"),
        (policy_id, "policy_id"),
    ):
        _require_machine_name(value, name)
    _require_digest(policy_digest, "policy_digest")
    keys = tuple(fact_keys)
    if not keys or any(type(key) is not FactKey for key in keys):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "fact_keys must contain at least one exact FactKey.",
        )
    if len(keys) > _MAX_RESULT_ITEMS:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            (
                "fact_keys cannot exceed "
                f"{_MAX_RESULT_ITEMS} items."
            ),
        )
    key_ids = sorted({key.key_id for key in keys})
    if len(key_ids) != len(keys):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "fact_keys cannot contain duplicates.",
        )
    normalized_scope = _normalize_named_scalars(
        query_scope,
        path="query_scope",
        secret_code="SECRET_IN_PROVIDER_REQUEST",
    )
    return _digest(
        {
            "provider_id": provider_id,
            "adapter_id": adapter_id,
            "adapter_version": adapter_version,
            "operation": operation,
            "policy_id": policy_id,
            "policy_digest": policy_digest,
            "fact_key_ids": key_ids,
            "query_scope": [
                [name, value] for name, value in normalized_scope
            ],
        },
        prefix="provider-request",
    )


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Exact trusted-host request envelope used by the promotion gate."""

    provider_id: str
    adapter_id: str
    adapter_version: str
    operation: str
    fact_keys: tuple[FactKey, ...]
    policy_id: str
    policy_digest: str
    query_scope: tuple[tuple[str, Scalar], ...] = field(
        default=(),
        repr=False,
    )
    request_fingerprint: str = ""

    def __post_init__(self) -> None:
        for value, name in (
            (self.provider_id, "ProviderRequest.provider_id"),
            (self.adapter_id, "ProviderRequest.adapter_id"),
            (
                self.adapter_version,
                "ProviderRequest.adapter_version",
            ),
            (self.operation, "ProviderRequest.operation"),
            (self.policy_id, "ProviderRequest.policy_id"),
        ):
            _require_machine_name(value, name)
        _require_digest(
            self.policy_digest, "ProviderRequest.policy_digest"
        )
        if (
            not isinstance(self.fact_keys, tuple)
            or not self.fact_keys
            or any(type(item) is not FactKey for item in self.fact_keys)
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "ProviderRequest.fact_keys must contain exact FactKey values.",
            )
        if len(self.fact_keys) > _MAX_RESULT_ITEMS:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                (
                    "ProviderRequest.fact_keys cannot exceed "
                    f"{_MAX_RESULT_ITEMS} items."
                ),
            )
        normalized_keys = tuple(
            sorted(self.fact_keys, key=lambda item: item.key_id)
        )
        if len({item.key_id for item in normalized_keys}) != len(
            normalized_keys
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "ProviderRequest.fact_keys cannot contain duplicates.",
            )
        normalized_scope = _normalize_named_scalars(
            self.query_scope,
            path="ProviderRequest.query_scope",
            secret_code="SECRET_IN_PROVIDER_REQUEST",
        )
        object.__setattr__(self, "fact_keys", normalized_keys)
        object.__setattr__(self, "query_scope", normalized_scope)
        expected = provider_request_fingerprint(
            provider_id=self.provider_id,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            operation=self.operation,
            fact_keys=normalized_keys,
            policy_id=self.policy_id,
            policy_digest=self.policy_digest,
            query_scope=normalized_scope,
        )
        if self.request_fingerprint and (
            self.request_fingerprint != expected
        ):
            raise FactContractError(
                "DIGEST_MISMATCH",
                "ProviderRequest fingerprint does not match request scope.",
            )
        object.__setattr__(self, "request_fingerprint", expected)

    @property
    def requested_key_ids(self) -> tuple[str, ...]:
        return tuple(item.key_id for item in self.fact_keys)

    def to_binding_dict(self) -> dict[str, Any]:
        """Return no user query values; safe for receipts and diagnostics."""

        return {
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "operation": self.operation,
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "requested_key_ids": list(self.requested_key_ids),
            "query_field_names": [
                name for name, _value in self.query_scope
            ],
            "request_fingerprint": self.request_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class FactObservation:
    """One trusted normalized observation with two independent deadlines."""

    key: FactKey
    value: FactValue = field(repr=False)
    provenance: ProviderProvenance = field(repr=False)
    retrieved_at: datetime
    valid_until: datetime
    purge_at: datetime | None
    confidence: float
    contract_version: str = FACT_OBSERVATION_VERSION
    observation_id: str = ""

    def __post_init__(self) -> None:
        if self.contract_version != FACT_OBSERVATION_VERSION:
            raise FactContractError(
                "UNSUPPORTED_VERSION",
                (
                    "Unsupported fact observation version "
                    f"{self.contract_version!r}."
                ),
            )
        if type(self.key) is not FactKey or type(self.value) is not FactValue:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "FactObservation requires exact FactKey and FactValue.",
            )
        if type(self.provenance) is not ProviderProvenance:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "FactObservation requires exact ProviderProvenance.",
            )
        if self.key.kind is not self.value.kind:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Fact key and normalized value kinds differ.",
            )
        retrieved_at = _aware_utc(
            self.retrieved_at, "FactObservation.retrieved_at"
        )
        valid_until = _aware_utc(
            self.valid_until, "FactObservation.valid_until"
        )
        if valid_until <= retrieved_at:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "FactObservation.valid_until must be after retrieved_at.",
            )
        purge_at = (
            None
            if self.purge_at is None
            else _aware_utc(self.purge_at, "FactObservation.purge_at")
        )
        if purge_at is None:
            if self.key.kind is not FactKind.PLACE_IDENTITY:
                raise FactContractError(
                    "INVALID_PROVIDER_RESPONSE",
                    "Only place identity may use a non-expiring retention policy.",
                )
        elif purge_at <= retrieved_at:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "FactObservation.purge_at must be after retrieved_at.",
            )
        try:
            normalized_confidence = float(self.confidence)
        except (OverflowError, TypeError, ValueError) as exc:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "FactObservation.confidence must be between 0 and 1.",
            ) from exc
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(normalized_confidence)
            or not 0.0 <= normalized_confidence <= 1.0
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "FactObservation.confidence must be between 0 and 1.",
            )
        object.__setattr__(self, "retrieved_at", retrieved_at)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "purge_at", purge_at)
        object.__setattr__(
            self, "confidence", normalized_confidence
        )
        _validate_key_value_binding(self.key, self.value)
        _validate_key_provenance_binding(self.key, self.provenance)
        _validate_observation_time_semantics(
            self.key,
            self.value,
            retrieved_at,
            valid_until,
        )
        expected_id = _digest(
            self._identity_payload(),
            prefix="fact-observation",
        )
        if self.observation_id and self.observation_id != expected_id:
            raise FactContractError(
                "DIGEST_MISMATCH",
                "FactObservation.observation_id does not match content.",
            )
        object.__setattr__(self, "observation_id", expected_id)

    @property
    def source_slot(self) -> tuple[str, str]:
        """One LKG slot per exact fact key and provider."""

        return (self.key.key_id, self.provenance.provider_id)

    def retained_at(self, purge_now: datetime) -> bool:
        checked = _aware_utc(purge_now, "purge_now")
        return self.purge_at is None or checked < self.purge_at

    def fresh_at(self, evaluation_at: datetime) -> bool:
        checked = _aware_utc(evaluation_at, "evaluation_at")
        return self.retrieved_at <= checked < self.valid_until

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "key": self.key.to_dict(),
            "value": self.value._identity_payload(),
            "provenance": self.provenance._identity_payload(),
            "retrieved_at": _utc_iso(self.retrieved_at),
            "valid_until": _utc_iso(self.valid_until),
            "purge_at": (
                _utc_iso(self.purge_at)
                if self.purge_at is not None
                else None
            ),
            "confidence": self.confidence,
        }

    def to_dict(self) -> dict[str, Any]:
        return _observation_binding(self)


@dataclass(frozen=True, slots=True)
class ProviderProblem:
    """Sanitized provider failure safe for durable diagnostics."""

    code: ProviderProblemCode
    message: str = field(repr=False)
    retryable: bool
    next_action: str
    fact_key_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, ProviderProblemCode):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "ProviderProblem.code must be ProviderProblemCode.",
            )
        normalized_message = _normalized_text(
            self.message, "ProviderProblem.message", maximum=1024
        )
        object.__setattr__(self, "message", normalized_message)
        if _SECRET_VALUE_RE.search(normalized_message):
            raise FactContractError(
                "SECRET_IN_PROVIDER_RESULT",
                "ProviderProblem.message cannot contain credential values.",
            )
        if not isinstance(self.retryable, bool):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "ProviderProblem.retryable must be bool.",
            )
        if self.retryable and self.code not in _AUTOMATIC_RETRY_CODES:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                (
                    f"Provider problem {self.code.value} is not safe for "
                    "automatic retry."
                ),
            )
        _require_machine_name(
            self.next_action, "ProviderProblem.next_action"
        )
        if not isinstance(self.fact_key_ids, tuple):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "ProviderProblem.fact_key_ids must be a tuple.",
            )
        if len(self.fact_key_ids) > _MAX_RESULT_ITEMS:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                (
                    "ProviderProblem.fact_key_ids cannot exceed "
                    f"{_MAX_RESULT_ITEMS} items."
                ),
            )
        for key_id in self.fact_key_ids:
            _require_digest(key_id, "ProviderProblem.fact_key_ids item")
        normalized = tuple(sorted(set(self.fact_key_ids)))
        object.__setattr__(self, "fact_key_ids", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "next_action": self.next_action,
            "fact_key_ids": list(self.fact_key_ids),
        }


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """One bounded provider outcome; generic empty success is impossible."""

    request_fingerprint: str
    status: ProviderResultStatus
    observations: tuple[FactObservation, ...] = field(repr=False)
    problems: tuple[ProviderProblem, ...] = field(repr=False)
    attempts_used: int
    completed_at: datetime
    contract_version: str = PROVIDER_RESULT_VERSION
    result_id: str = ""

    def __post_init__(self) -> None:
        if self.contract_version != PROVIDER_RESULT_VERSION:
            raise FactContractError(
                "UNSUPPORTED_VERSION",
                f"Unsupported provider result version {self.contract_version!r}.",
            )
        _require_digest(self.request_fingerprint, "request_fingerprint")
        if not isinstance(self.status, ProviderResultStatus):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "ProviderResult.status must be ProviderResultStatus.",
            )
        if not isinstance(self.observations, tuple) or any(
            type(item) is not FactObservation
            for item in self.observations
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "ProviderResult.observations must contain exact observations.",
            )
        if len(self.observations) > _MAX_RESULT_ITEMS:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                (
                    "ProviderResult.observations cannot exceed "
                    f"{_MAX_RESULT_ITEMS} items."
                ),
            )
        if not isinstance(self.problems, tuple) or any(
            type(item) is not ProviderProblem for item in self.problems
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "ProviderResult.problems must contain exact problems.",
            )
        if len(self.problems) > _MAX_RESULT_ITEMS:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                (
                    "ProviderResult.problems cannot exceed "
                    f"{_MAX_RESULT_ITEMS} items."
                ),
            )
        if (
            isinstance(self.attempts_used, bool)
            or not isinstance(self.attempts_used, int)
            or self.attempts_used < 0
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "ProviderResult.attempts_used must be non-negative.",
            )
        completed_at = _aware_utc(
            self.completed_at, "ProviderResult.completed_at"
        )
        object.__setattr__(self, "completed_at", completed_at)
        observation_ids = [
            observation.observation_id
            for observation in self.observations
        ]
        if len(set(observation_ids)) != len(observation_ids):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "ProviderResult contains duplicate observations.",
            )
        source_slots = [
            observation.source_slot
            for observation in self.observations
        ]
        if len(set(source_slots)) != len(source_slots):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "ProviderResult cannot contain duplicate provider LKG slots.",
            )
        for observation in self.observations:
            if (
                observation.provenance.request_fingerprint
                != self.request_fingerprint
            ):
                raise FactContractError(
                    "EVIDENCE_BINDING_MISMATCH",
                    "Observation request fingerprint differs from result.",
                )
            if observation.retrieved_at > completed_at:
                raise FactContractError(
                    "INVALID_PROVIDER_RESPONSE",
                    "Observation cannot be retrieved after result completion.",
                )
        sources = {
            (
                observation.provenance.provider_id,
                observation.provenance.adapter_id,
                observation.provenance.adapter_version,
            )
            for observation in self.observations
        }
        if len(sources) > 1:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "One ProviderResult cannot mix adapter source identities.",
            )
        if self.status in {
            ProviderResultStatus.SUCCESS,
            ProviderResultStatus.PARTIAL,
            ProviderResultStatus.CACHE_HIT,
        }:
            for observation in self.observations:
                if not observation.fresh_at(completed_at) or not (
                    observation.purge_at is None
                    or completed_at < observation.purge_at
                ):
                    raise FactContractError(
                        "INVALID_PROVIDER_RESPONSE",
                        (
                            "Successful provider results must contain fresh, "
                            "retained evidence at completion."
                        ),
                    )
        if self.attempts_used == 0 and any(
            problem.code in _OUTBOUND_FAILURE_CODES
            for problem in self.problems
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Outbound provider failures must consume an attempt.",
            )
        _validate_result_shape(
            self.status,
            self.observations,
            self.problems,
            self.attempts_used,
        )
        normalized_observations = tuple(
            sorted(
                self.observations,
                key=lambda item: item.observation_id,
            )
        )
        normalized_problems = tuple(
            sorted(
                self.problems,
                key=lambda item: (
                    item.code.value,
                    item.fact_key_ids,
                    item.message,
                ),
            )
        )
        object.__setattr__(self, "observations", normalized_observations)
        object.__setattr__(self, "problems", normalized_problems)
        expected_id = _digest(
            self._identity_payload(),
            prefix="provider-result",
        )
        if self.result_id and self.result_id != expected_id:
            raise FactContractError(
                "DIGEST_MISMATCH",
                "ProviderResult.result_id does not match content.",
            )
        object.__setattr__(self, "result_id", expected_id)

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "request_fingerprint": self.request_fingerprint,
            "status": self.status.value,
            "observation_ids": [
                item.observation_id for item in self.observations
            ],
            "problems": [item.to_dict() for item in self.problems],
            "attempts_used": self.attempts_used,
            "completed_at": _utc_iso(self.completed_at),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._identity_payload(),
            "observation_bindings": [
                _observation_binding(item)
                for item in self.observations
            ],
            "result_id": self.result_id,
        }


_AUTHORIZATION_TOKEN = object()
_GOOGLE_PLACE_IDENTITY_AUTHORIZATION_TOKEN = object()
_GOOGLE_ROUTE_AUTHORIZATION_TOKEN = object()
_GENERIC_AUTHORIZATION_GATE = "provider-policy"
_GOOGLE_PLACE_IDENTITY_AUTHORIZATION_GATE = "google-place-identity"
_GOOGLE_ROUTE_AUTHORIZATION_GATE = "google-route"
_GOOGLE_ROUTE_REQUIRED_QUERY_FIELDS = frozenset(
    {
        "basis_evidence_revision",
        "basis_snapshot_id",
        "basis_store_revision",
        "destination_endpoint_id",
        "destination_observation_id",
        "destination_value_digest",
        "field_mask",
        "origin_endpoint_id",
        "origin_observation_id",
        "origin_value_digest",
    }
)
_GOOGLE_ROUTE_OPTIONAL_QUERY_FIELDS = frozenset({"fallback_from_mode"})


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedProviderResult:
    """A raw provider result that passed one exact trusted policy gate."""

    request: ProviderRequest
    result: ProviderResult = field(repr=False)
    policy_registry_revision: str
    authorization_id: str

    def __init__(
        self,
        *,
        request: ProviderRequest,
        result: ProviderResult,
        policy_registry_revision: str,
        authorization_id: str,
        _token: object,
    ) -> None:
        if _token is not _AUTHORIZATION_TOKEN:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "AuthorizedProviderResult can only be created by the host gate.",
            )
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "result", result)
        object.__setattr__(
            self,
            "policy_registry_revision",
            policy_registry_revision,
        )
        object.__setattr__(self, "authorization_id", authorization_id)

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_binding_dict(),
            "result_id": self.result.result_id,
            "policy_registry_revision": self.policy_registry_revision,
            "authorization_id": self.authorization_id,
        }


def authorize_provider_result(
    request: ProviderRequest,
    result: ProviderResult,
    policies: ProviderPolicyRegistry,
) -> AuthorizedProviderResult:
    """Validate a generic provider result.

    Google place identity is deliberately excluded: only the dedicated
    review/refresh finalizers may mint an authorized identity result.
    """

    return _authorize_provider_result(
        request,
        result,
        policies,
        authorization_gate=_GENERIC_AUTHORIZATION_GATE,
    )


def _authorize_google_place_identity_result(
    request: ProviderRequest,
    result: ProviderResult,
    policies: ProviderPolicyRegistry,
    *,
    _token: object,
) -> AuthorizedProviderResult:
    """Authorize one identity result minted by the dedicated boundary."""

    if _token is not _GOOGLE_PLACE_IDENTITY_AUTHORIZATION_TOKEN:
        raise FactContractError(
            "PENDING_REVIEW",
            "Google place identity authorization requires a trusted finalizer.",
        )
    if not _is_google_place_identity_promotion(request):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Dedicated identity authorization received a non-identity request.",
        )
    return _authorize_provider_result(
        request,
        result,
        policies,
        authorization_gate=_GOOGLE_PLACE_IDENTITY_AUTHORIZATION_GATE,
        _identity_token=_token,
    )


def _authorize_google_route_result(
    request: ProviderRequest,
    result: ProviderResult,
    policies: ProviderPolicyRegistry,
    *,
    _token: object,
) -> AuthorizedProviderResult:
    """Authorize one Google route result minted by the dedicated adapter."""

    if _token is not _GOOGLE_ROUTE_AUTHORIZATION_TOKEN:
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Google route authorization requires the trusted route adapter.",
        )
    if not _is_google_route_promotion(request):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Dedicated route authorization received a non-route request.",
        )
    _validate_google_route_authorization_scope(request, result)
    return _authorize_provider_result(
        request,
        result,
        policies,
        authorization_gate=_GOOGLE_ROUTE_AUTHORIZATION_GATE,
        _route_token=_token,
    )


def _authorize_provider_result(
    request: ProviderRequest,
    result: ProviderResult,
    policies: ProviderPolicyRegistry,
    *,
    authorization_gate: str,
    _identity_token: object | None = None,
    _route_token: object | None = None,
) -> AuthorizedProviderResult:
    """Shared exact policy validation behind token-separated host gates."""

    if (
        authorization_gate == _GOOGLE_PLACE_IDENTITY_AUTHORIZATION_GATE
        and _identity_token is not _GOOGLE_PLACE_IDENTITY_AUTHORIZATION_TOKEN
    ):
        raise FactContractError(
            "PENDING_REVIEW",
            "Google place identity authorization requires a trusted finalizer.",
        )
    if (
        authorization_gate == _GOOGLE_ROUTE_AUTHORIZATION_GATE
        and _route_token is not _GOOGLE_ROUTE_AUTHORIZATION_TOKEN
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Google route authorization requires the trusted route adapter.",
        )
    if (
        type(request) is not ProviderRequest
        or type(result) is not ProviderResult
        or type(policies) is not ProviderPolicyRegistry
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Provider promotion requires exact trusted contract values.",
        )
    policy = policies.policy(request.policy_id)
    if request.policy_digest != policy.policy_digest:
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "ProviderRequest policy digest is no longer allowlisted.",
        )
    source = (
        request.provider_id,
        request.adapter_id,
        request.adapter_version,
    )
    expected_source = (
        policy.provider_id,
        policy.adapter_id,
        policy.adapter_version,
    )
    if source != expected_source:
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "ProviderRequest source is outside its policy.",
        )
    if request.operation not in policy.allowed_operations:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "ProviderRequest operation is outside its static policy.",
        )
    if any(
        key.kind not in policy.allowed_fact_kinds
        for key in request.fact_keys
    ):
        raise FactContractError(
            "UNSUPPORTED_FACT_KIND",
            "ProviderRequest contains a fact kind outside its policy.",
        )
    query_names = {name for name, _value in request.query_scope}
    if not query_names.issubset(policy.allowed_query_fields):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "ProviderRequest query scope contains non-allowlisted fields.",
        )
    if result.request_fingerprint != request.request_fingerprint:
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            "ProviderResult does not bind to the exact ProviderRequest.",
        )

    requested = set(request.requested_key_ids)
    observation_keys: set[str] = set()
    for observation in result.observations:
        if observation.key.key_id not in requested:
            raise FactContractError(
                "OUT_OF_SCOPE_RESULT",
                "ProviderResult contains an unrequested fact observation.",
            )
        provenance = observation.provenance
        if (
            provenance.request_fingerprint
            != request.request_fingerprint
            or (
                provenance.provider_id,
                provenance.adapter_id,
                provenance.adapter_version,
            )
            != source
            or provenance.retention_policy_id != policy.policy_id
        ):
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Observation provenance differs from the authorized request.",
            )
        policies.validate_observation(observation)
        observation_keys.add(observation.key.key_id)

    problem_keys: set[str] = set()
    has_batch_problem = False
    for problem in result.problems:
        if not problem.fact_key_ids:
            has_batch_problem = True
            continue
        keys = set(problem.fact_key_ids)
        if not keys.issubset(requested):
            raise FactContractError(
                "OUT_OF_SCOPE_RESULT",
                "ProviderResult problem references an unrequested fact key.",
            )
        problem_keys.update(keys)

    if result.status in {
        ProviderResultStatus.SUCCESS,
        ProviderResultStatus.CACHE_HIT,
    }:
        complete = observation_keys == requested
    elif result.status is ProviderResultStatus.PARTIAL:
        complete = (
            not has_batch_problem
            and not observation_keys.intersection(problem_keys)
            and observation_keys.union(problem_keys) == requested
        )
    else:
        complete = has_batch_problem or problem_keys == requested
    if not complete:
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            "ProviderResult does not exactly cover its requested fact keys.",
        )
    if (
        authorization_gate == _GENERIC_AUTHORIZATION_GATE
        and _is_google_place_identity_promotion(request)
    ):
        raise FactContractError(
            "PENDING_REVIEW",
            (
                "Google place identity requires the dedicated reviewed "
                "promotion boundary."
            ),
        )
    if (
        authorization_gate == _GENERIC_AUTHORIZATION_GATE
        and _is_google_route_promotion(request)
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            (
                "Google route evidence requires the dedicated adapter "
                "promotion boundary."
            ),
        )

    authorization_id = _digest(
        {
            "authorization_gate": authorization_gate,
            "request_fingerprint": request.request_fingerprint,
            "result_id": result.result_id,
            "policy_digest": policy.policy_digest,
            "policy_registry_revision": policies.revision,
        },
        prefix="authorized-provider-result",
    )
    return AuthorizedProviderResult(
        request=request,
        result=result,
        policy_registry_revision=policies.revision,
        authorization_id=authorization_id,
        _token=_AUTHORIZATION_TOKEN,
    )


def _is_google_place_identity_promotion(
    request: object,
) -> bool:
    return bool(
        type(request) is ProviderRequest
        and request.provider_id == "google-places"
        and request.policy_id == "google-place-id-v1"
        and request.operation in {"refresh-place-id", "resolve-place"}
        and any(
            key.kind is FactKind.PLACE_IDENTITY
            for key in request.fact_keys
        )
    )


def _is_google_route_promotion(request: object) -> bool:
    return bool(
        type(request) is ProviderRequest
        and request.provider_id == "google-routes"
        and request.policy_id == "google-route-runtime-v1"
        and request.operation == "compute-route"
        and any(
            key.kind is FactKind.ROUTE_ESTIMATE
            for key in request.fact_keys
        )
    )


def _validate_google_route_authorization_scope(
    request: ProviderRequest,
    result: ProviderResult,
) -> None:
    if (
        len(request.fact_keys) != 1
        or request.fact_keys[0].kind is not FactKind.ROUTE_ESTIMATE
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Google route authorization requires exactly one route fact key.",
        )
    key = request.fact_keys[0]
    if key.subject_ids[0] == key.subject_ids[1]:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Google route endpoints must be distinct ordered locations.",
        )
    qualifiers = key.qualifier_map
    if set(qualifiers) != {"departure_at", "mode"}:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            (
                "Google route facts require exact mode and departure_at "
                "qualifiers."
            ),
        )

    scope = dict(request.query_scope)
    scope_names = set(scope)
    allowed_scope = (
        _GOOGLE_ROUTE_REQUIRED_QUERY_FIELDS
        | _GOOGLE_ROUTE_OPTIONAL_QUERY_FIELDS
    )
    if (
        not _GOOGLE_ROUTE_REQUIRED_QUERY_FIELDS.issubset(scope_names)
        or not scope_names.issubset(allowed_scope)
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            (
                "Google route request scope must contain the exact endpoint, "
                "evidence basis and field-mask bindings."
            ),
        )
    for name in _GOOGLE_ROUTE_REQUIRED_QUERY_FIELDS - {"field_mask"}:
        if (
            not isinstance(scope[name], str)
            or _HEX_DIGEST_RE.fullmatch(scope[name]) is None
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                (
                    f"Google route scope {name} must be a lowercase "
                    "SHA-256 digest."
                ),
            )
    field_mask = scope["field_mask"]
    if field_mask != _GOOGLE_ROUTE_FIELD_MASK:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Google route field_mask must equal the fixed minimal field mask.",
        )

    mode = qualifiers["mode"]
    fallback = scope.get("fallback_from_mode")
    if fallback is not None and (
        fallback != "transit" or mode != "driving"
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            (
                "Only an exact transit-to-driving Google route fallback "
                "is supported."
            ),
        )
    for observation in result.observations:
        payload = observation.value.payload
        payload_fallback = payload.get("fallback_from_mode")
        if payload["mode"] != mode or payload_fallback != fallback:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                (
                    "Google route observation mode or fallback scope differs "
                    "from its exact request."
                ),
            )


_LEDGER_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class EvidenceLedger:
    """Pure current-state LKG set; durable I/O belongs to EvidenceStore."""

    policies: ProviderPolicyRegistry
    observations: tuple[FactObservation, ...] = field(
        default=(),
        repr=False,
    )
    generation: int = 0
    revision: str = ""

    def __init__(
        self,
        policies: ProviderPolicyRegistry,
        observations: tuple[FactObservation, ...] = (),
        generation: int = 0,
        revision: str = "",
        *,
        _token: object | None = None,
    ) -> None:
        if observations and _token is not _LEDGER_TOKEN:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                (
                    "Non-empty evidence ledgers can only be created by the "
                    "trusted promotion boundary."
                ),
            )
        object.__setattr__(self, "policies", policies)
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "revision", revision)
        self.__post_init__()

    def __post_init__(self) -> None:
        if type(self.policies) is not ProviderPolicyRegistry:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "EvidenceLedger requires an exact host policy registry.",
            )
        if not isinstance(self.observations, tuple) or any(
            type(item) is not FactObservation
            for item in self.observations
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "EvidenceLedger.observations must contain exact observations.",
            )
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
            or self.generation > _MAX_GENERATION
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "EvidenceLedger.generation is outside the supported range.",
            )
        slots: set[tuple[str, str]] = set()
        for observation in self.observations:
            self.policies.validate_observation(observation)
            if observation.source_slot in slots:
                raise FactContractError(
                    "CACHE_CORRUPTED",
                    "EvidenceLedger contains duplicate provider LKG slots.",
                )
            slots.add(observation.source_slot)
        normalized = tuple(
            sorted(
                self.observations,
                key=lambda item: (
                    item.key.key_id,
                    item.provenance.provider_id,
                    item.observation_id,
                ),
            )
        )
        object.__setattr__(self, "observations", normalized)
        expected_revision = _digest(
            {
                "policy_registry_revision": self.policies.revision,
                "generation": self.generation,
                "observation_ids": [
                    item.observation_id for item in normalized
                ],
            },
            prefix="evidence-ledger",
        )
        if self.revision and self.revision != expected_revision:
            raise FactContractError(
                "DIGEST_MISMATCH",
                "EvidenceLedger.revision does not match current LKG state.",
            )
        object.__setattr__(self, "revision", expected_revision)

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted binding, never provider value bytes."""

        return {
            "policy_registry_revision": self.policies.revision,
            "generation": self.generation,
            "revision": self.revision,
            "observation_bindings": [
                _observation_binding(
                    observation,
                    policy=self.policies.policy(
                        observation.provenance.retention_policy_id
                    ),
                )
                for observation in self.observations
            ],
        }

    def durable_observations(
        self,
        *,
        purge_now: datetime,
    ) -> tuple[FactObservation, ...]:
        """Return only records explicitly authorized to cross process bounds."""

        checked_at = _aware_utc(
            purge_now, "EvidenceLedger.durable_observations.purge_now"
        )
        return tuple(
            observation
            for observation in self.observations
            if self.policies.persistence_for(observation)
            is not EvidencePersistence.MEMORY_ONLY
            and observation.retained_at(checked_at)
        )


@dataclass(frozen=True, slots=True)
class EvidencePrune:
    """Result of applying one trusted compliance clock to a ledger."""

    ledger: EvidenceLedger
    changed: bool
    purged_observation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.ledger) is not EvidenceLedger:
            raise FactContractError(
                "CACHE_CORRUPTED",
                "EvidencePrune.ledger must be an exact EvidenceLedger.",
            )
        if not isinstance(self.changed, bool):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "EvidencePrune.changed must be bool.",
            )
        if not isinstance(self.purged_observation_ids, tuple):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "EvidencePrune.purged_observation_ids must be a tuple.",
            )
        for observation_id in self.purged_observation_ids:
            _require_digest(
                observation_id,
                "EvidencePrune.purged_observation_ids item",
            )
        normalized = tuple(sorted(set(self.purged_observation_ids)))
        object.__setattr__(
            self, "purged_observation_ids", normalized
        )
        if self.changed != bool(normalized):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "EvidencePrune change flag must match its purged records.",
            )


def prune_evidence(
    ledger: EvidenceLedger,
    *,
    purge_now: datetime,
) -> EvidencePrune:
    """Remove retention-expired records without inventing a provider result."""

    if type(ledger) is not EvidenceLedger:
        raise FactContractError(
            "CACHE_CORRUPTED",
            "prune_evidence requires an exact EvidenceLedger.",
        )
    checked_at = _aware_utc(purge_now, "prune_evidence.purge_now")
    retained = tuple(
        observation
        for observation in ledger.observations
        if observation.retained_at(checked_at)
    )
    purged = tuple(
        observation.observation_id
        for observation in ledger.observations
        if not observation.retained_at(checked_at)
    )
    if not purged:
        return EvidencePrune(ledger=ledger, changed=False)
    return EvidencePrune(
        ledger=EvidenceLedger(
            policies=ledger.policies,
            observations=retained,
            generation=ledger.generation + 1,
            _token=_LEDGER_TOKEN,
        ),
        changed=True,
        purged_observation_ids=purged,
    )


def _restore_durable_evidence_ledger(
    *,
    policies: ProviderPolicyRegistry,
    observations: tuple[FactObservation, ...],
    generation: int,
    revision: str = "",
) -> EvidenceLedger:
    """Narrow package-internal restore boundary for a strict disk codec."""

    if type(policies) is not ProviderPolicyRegistry:
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Durable restore requires exact host policies.",
        )
    if not isinstance(observations, tuple) or any(
        type(item) is not FactObservation for item in observations
    ):
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Durable restore requires exact observations.",
        )
    for observation in observations:
        if (
            policies.persistence_for(observation)
            is EvidencePersistence.MEMORY_ONLY
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "Memory-only provider content cannot be restored from disk.",
            )
    return EvidenceLedger(
        policies=policies,
        observations=observations,
        generation=generation,
        revision=revision,
        _token=_LEDGER_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class EvidenceMerge:
    """Pure result of pruning and merging one provider outcome."""

    ledger: EvidenceLedger
    changed: bool
    purged_observation_ids: tuple[str, ...] = ()
    promoted_observation_ids: tuple[str, ...] = ()
    ignored_observation_ids: tuple[str, ...] = ()
    problems: tuple[ProviderProblem, ...] = ()

    def __post_init__(self) -> None:
        if type(self.ledger) is not EvidenceLedger:
            raise FactContractError(
                "CACHE_CORRUPTED",
                "EvidenceMerge.ledger must be exact EvidenceLedger.",
            )
        if not isinstance(self.changed, bool):
            raise FactContractError(
                "CACHE_CORRUPTED", "EvidenceMerge.changed must be bool."
            )
        for value, name in (
            (
                self.purged_observation_ids,
                "purged_observation_ids",
            ),
            (
                self.promoted_observation_ids,
                "promoted_observation_ids",
            ),
            (
                self.ignored_observation_ids,
                "ignored_observation_ids",
            ),
        ):
            if not isinstance(value, tuple):
                raise FactContractError(
                    "CACHE_CORRUPTED", f"{name} must be a tuple."
                )
            for observation_id in value:
                _require_digest(observation_id, f"{name} item")
            object.__setattr__(self, name, tuple(sorted(set(value))))
        if not isinstance(self.problems, tuple) or any(
            type(item) is not ProviderProblem for item in self.problems
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "EvidenceMerge.problems must contain exact problems.",
            )


def merge_provider_result(
    ledger: EvidenceLedger,
    authorized_result: AuthorizedProviderResult,
    *,
    purge_now: datetime,
) -> EvidenceMerge:
    """Prune expired content and merge only successful exact observations."""

    if (
        type(ledger) is not EvidenceLedger
        or type(authorized_result) is not AuthorizedProviderResult
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "merge requires an EvidenceLedger and authorized provider result.",
        )
    if (
        ledger.policies.revision
        != authorized_result.policy_registry_revision
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Provider result was authorized under a different policy revision.",
        )
    if _is_google_place_identity_promotion(authorized_result.request):
        reauthorized = _authorize_google_place_identity_result(
            authorized_result.request,
            authorized_result.result,
            ledger.policies,
            _token=_GOOGLE_PLACE_IDENTITY_AUTHORIZATION_TOKEN,
        )
    elif _is_google_route_promotion(authorized_result.request):
        reauthorized = _authorize_google_route_result(
            authorized_result.request,
            authorized_result.result,
            ledger.policies,
            _token=_GOOGLE_ROUTE_AUTHORIZATION_TOKEN,
        )
    else:
        reauthorized = authorize_provider_result(
            authorized_result.request,
            authorized_result.result,
            ledger.policies,
        )
    if (
        reauthorized.authorization_id
        != authorized_result.authorization_id
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Authorized provider result binding is invalid.",
        )
    result = authorized_result.result
    checked_at = _aware_utc(purge_now, "purge_now")
    if result.completed_at > checked_at:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "ProviderResult.completed_at cannot be after purge_now.",
        )

    slots: dict[tuple[str, str], FactObservation] = {}
    purged: list[str] = []
    for observation in ledger.observations:
        if observation.retained_at(checked_at):
            slots[observation.source_slot] = observation
        else:
            purged.append(observation.observation_id)

    promoted: list[str] = []
    ignored: list[str] = []
    generated_problems: list[ProviderProblem] = []
    for observation in result.observations:
        if not observation.retained_at(checked_at):
            ignored.append(observation.observation_id)
            generated_problems.append(
                ProviderProblem(
                    code=ProviderProblemCode.RETENTION_EXPIRED,
                    message=(
                        "Normalized provider content reached its retention "
                        "deadline before promotion."
                    ),
                    retryable=False,
                    next_action="refresh_evidence",
                    fact_key_ids=(observation.key.key_id,),
                )
            )
            continue
        slot = observation.source_slot
        previous = slots.get(slot)
        if (
            previous is not None
            and previous.observation_id == observation.observation_id
        ):
            ignored.append(observation.observation_id)
            continue
        if not _place_identity_basis_matches(
            authorized_result.request,
            observation,
            previous,
        ):
            ignored.append(observation.observation_id)
            generated_problems.append(
                ProviderProblem(
                    code=ProviderProblemCode.EVIDENCE_REVISION_CHANGED,
                    message=(
                        "Place identity changed after the provider request "
                        "was bound to its last-known-good basis."
                    ),
                    retryable=False,
                    next_action="review_current_identity",
                    fact_key_ids=(observation.key.key_id,),
                )
            )
            continue
        if previous is None:
            if result.status is ProviderResultStatus.CACHE_HIT:
                raise FactContractError(
                    "INVALID_PROVIDER_RESPONSE",
                    "A cache-hit result cannot seed a missing LKG slot.",
                )
            slots[slot] = observation
            promoted.append(observation.observation_id)
            continue
        if result.status is ProviderResultStatus.CACHE_HIT:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "A cache-hit result must replay the exact cached observation.",
            )
        if observation.retrieved_at < previous.retrieved_at:
            ignored.append(observation.observation_id)
            generated_problems.append(
                ProviderProblem(
                    code=ProviderProblemCode.STALE_PROVIDER_RESULT,
                    message=(
                        "An older provider result cannot replace the current "
                        "last-known-good observation."
                    ),
                    retryable=False,
                    next_action="keep_last_known_good",
                    fact_key_ids=(observation.key.key_id,),
                )
            )
            continue
        if observation.retrieved_at == previous.retrieved_at:
            raise FactContractError(
                "AMBIGUOUS_PROVIDER_RESULT",
                (
                    "The same provider produced different observations for one "
                    "fact at the same retrieval time."
                ),
            )
        slots[slot] = observation
        promoted.append(observation.observation_id)

    normalized = tuple(
        sorted(
            slots.values(),
            key=lambda item: (
                item.key.key_id,
                item.provenance.provider_id,
                item.observation_id,
            ),
        )
    )
    before_ids = tuple(
        item.observation_id for item in ledger.observations
    )
    after_ids = tuple(item.observation_id for item in normalized)
    changed = before_ids != after_ids
    next_ledger = (
        EvidenceLedger(
            policies=ledger.policies,
            observations=normalized,
            generation=ledger.generation + 1,
            _token=_LEDGER_TOKEN,
        )
        if changed
        else ledger
    )
    return EvidenceMerge(
        ledger=next_ledger,
        changed=changed,
        purged_observation_ids=tuple(purged),
        promoted_observation_ids=tuple(promoted),
        ignored_observation_ids=tuple(ignored),
        problems=tuple(result.problems) + tuple(generated_problems),
    )


def _place_identity_basis_matches(
    request: ProviderRequest,
    incoming: FactObservation,
    previous: FactObservation | None,
) -> bool:
    if (
        incoming.key.kind is not FactKind.PLACE_IDENTITY
        or request.policy_id != "google-place-id-v1"
        or request.operation not in {"refresh-place-id", "resolve-place"}
    ):
        return True
    scope = dict(request.query_scope)
    basis_fields = (
        "basis_observation_id",
        "basis_provider_place_id",
        "basis_value_digest",
    )
    present = tuple(field in scope for field in basis_fields)
    if any(present) and not all(present):
        return False
    if not any(present):
        return (
            request.operation == "resolve-place"
            and previous is None
        )
    if previous is None:
        return False
    return (
        scope["basis_observation_id"] == previous.observation_id
        and scope["basis_provider_place_id"]
        == previous.value.payload["provider_place_id"]
        and scope["basis_value_digest"] == previous.value.value_digest
    )


_RESOLUTION_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class FactResolution:
    """One exact fact resolved without hiding staleness or disagreement."""

    key: FactKey
    evidence_state: EvidenceState
    reason: ResolutionReason
    selected: FactObservation | None = field(repr=False)
    candidates: tuple[FactObservation, ...] = field(repr=False)
    evaluation_at: datetime
    purge_checked_at: datetime
    snapshot_id: str

    def __init__(
        self,
        *,
        key: FactKey,
        evidence_state: EvidenceState,
        reason: ResolutionReason,
        selected: FactObservation | None,
        candidates: tuple[FactObservation, ...],
        evaluation_at: datetime,
        purge_checked_at: datetime,
        snapshot_id: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _RESOLUTION_TOKEN:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "FactResolution can only be created by EvidenceSnapshot.",
            )
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "evidence_state", evidence_state)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "selected", selected)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "evaluation_at", evaluation_at)
        object.__setattr__(
            self, "purge_checked_at", purge_checked_at
        )
        object.__setattr__(self, "snapshot_id", snapshot_id)
        self.__post_init__()

    def __post_init__(self) -> None:
        if type(self.key) is not FactKey:
            raise FactContractError(
                "CACHE_CORRUPTED", "FactResolution.key must be exact FactKey."
            )
        if not isinstance(self.evidence_state, EvidenceState):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "FactResolution.evidence_state must be EvidenceState.",
            )
        if not isinstance(self.reason, ResolutionReason):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "FactResolution.reason must be ResolutionReason.",
            )
        if self.selected is not None and type(self.selected) is not FactObservation:
            raise FactContractError(
                "CACHE_CORRUPTED",
                "FactResolution.selected must be an observation or None.",
            )
        if not isinstance(self.candidates, tuple) or any(
            type(item) is not FactObservation for item in self.candidates
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "FactResolution.candidates must contain exact observations.",
            )
        evaluation_at = _aware_utc(
            self.evaluation_at, "FactResolution.evaluation_at"
        )
        purge_checked_at = _aware_utc(
            self.purge_checked_at,
            "FactResolution.purge_checked_at",
        )
        _require_digest(self.snapshot_id, "FactResolution.snapshot_id")
        object.__setattr__(self, "evaluation_at", evaluation_at)
        object.__setattr__(
            self, "purge_checked_at", purge_checked_at
        )
        candidate_ids = tuple(
            item.observation_id for item in self.candidates
        )
        if len(set(candidate_ids)) != len(candidate_ids):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "FactResolution.candidates cannot contain duplicates.",
            )
        if any(
            item.key.key_id != self.key.key_id
            for item in self.candidates
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "FactResolution candidates must bind to its exact fact key.",
            )
        if any(
            item.retrieved_at > evaluation_at
            or not item.retained_at(purge_checked_at)
            or not item.retained_at(evaluation_at)
            for item in self.candidates
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "FactResolution candidates are not visible in its snapshot.",
            )
        expected_reason = {
            EvidenceState.VERIFIED: ResolutionReason.FRESH,
            EvidenceState.STALE: ResolutionReason.STALE,
            EvidenceState.CONFLICTED: ResolutionReason.CONFLICTED,
            EvidenceState.UNVERIFIED: ResolutionReason.MISSING,
        }[self.evidence_state]
        if self.reason is not expected_reason:
            raise FactContractError(
                "CACHE_CORRUPTED",
                "FactResolution reason and evidence state disagree.",
            )
        if self.evidence_state is EvidenceState.CONFLICTED:
            distinct_values = {
                item.value.value_digest for item in self.candidates
            }
            if (
                self.selected is not None
                or len(self.candidates) < 2
                or len(distinct_values) < 2
            ):
                raise FactContractError(
                    "CACHE_CORRUPTED",
                    "Conflicted facts require material disagreement and no winner.",
                )
        elif self.evidence_state is EvidenceState.UNVERIFIED:
            if self.selected is not None or self.candidates:
                raise FactContractError(
                    "CACHE_CORRUPTED",
                    "Missing facts cannot expose an observation.",
                )
        elif (
            self.selected is None
            or not self.candidates
            or self.selected.observation_id not in candidate_ids
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "Fresh or stale facts require a selected candidate LKG.",
            )
        if self.evidence_state is EvidenceState.VERIFIED and not (
            self.selected is not None
            and self.selected.fresh_at(evaluation_at)
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "Verified resolution requires fresh selected evidence.",
            )
        if self.evidence_state is EvidenceState.STALE and (
            self.selected is None
            or self.selected.fresh_at(evaluation_at)
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "Stale resolution cannot select fresh evidence.",
            )

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(
            f"fact:{item.observation_id}" for item in self.candidates
        )

    @property
    def supports_travel_ready_use(self) -> bool:
        """Whether V1 may use this fact for a date-specific hard decision."""

        if (
            self.evidence_state is not EvidenceState.VERIFIED
            or self.selected is None
        ):
            return False
        if self.key.kind is FactKind.PLACE_OPENING_HOURS:
            return self.selected.value.payload["basis"] == "current"
        return True


_SNAPSHOT_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class EvidenceSnapshot:
    """Immutable evidence view at one semantic time and one purge check."""

    policies: ProviderPolicyRegistry
    observations: tuple[FactObservation, ...] = field(repr=False)
    evaluation_at: datetime
    purge_checked_at: datetime
    store_revision: str
    contract_version: str = EVIDENCE_SNAPSHOT_VERSION
    evidence_revision: str = ""
    outcome_revision: str | None = None
    snapshot_id: str = ""

    def __init__(
        self,
        *,
        policies: ProviderPolicyRegistry,
        observations: tuple[FactObservation, ...],
        evaluation_at: datetime,
        purge_checked_at: datetime,
        store_revision: str,
        contract_version: str = EVIDENCE_SNAPSHOT_VERSION,
        evidence_revision: str = "",
        outcome_revision: str | None = None,
        snapshot_id: str = "",
        _token: object | None = None,
    ) -> None:
        if _token is not _SNAPSHOT_TOKEN:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "EvidenceSnapshot can only be created from a trusted ledger.",
            )
        object.__setattr__(self, "policies", policies)
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "evaluation_at", evaluation_at)
        object.__setattr__(
            self, "purge_checked_at", purge_checked_at
        )
        object.__setattr__(self, "store_revision", store_revision)
        object.__setattr__(
            self, "contract_version", contract_version
        )
        object.__setattr__(
            self, "evidence_revision", evidence_revision
        )
        object.__setattr__(self, "outcome_revision", outcome_revision)
        object.__setattr__(self, "snapshot_id", snapshot_id)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.contract_version != EVIDENCE_SNAPSHOT_VERSION:
            raise FactContractError(
                "UNSUPPORTED_VERSION",
                (
                    "Unsupported evidence snapshot version "
                    f"{self.contract_version!r}."
                ),
            )
        if type(self.policies) is not ProviderPolicyRegistry:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "EvidenceSnapshot requires exact host provider policies.",
            )
        if not isinstance(self.observations, tuple) or any(
            type(item) is not FactObservation
            for item in self.observations
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "EvidenceSnapshot.observations must be exact observations.",
            )
        evaluation_at = _aware_utc(
            self.evaluation_at, "EvidenceSnapshot.evaluation_at"
        )
        purge_checked_at = _aware_utc(
            self.purge_checked_at,
            "EvidenceSnapshot.purge_checked_at",
        )
        _require_digest(
            self.store_revision, "EvidenceSnapshot.store_revision"
        )
        if self.outcome_revision is not None:
            _require_digest(
                self.outcome_revision,
                "EvidenceSnapshot.outcome_revision",
            )
        retained: list[FactObservation] = []
        slots: set[tuple[str, str]] = set()
        for observation in self.observations:
            self.policies.validate_observation(observation)
            if not observation.retained_at(purge_checked_at):
                raise FactContractError(
                    "RETENTION_EXPIRED",
                    "EvidenceSnapshot cannot contain expired provider content.",
                )
            if observation.retrieved_at > purge_checked_at:
                raise FactContractError(
                    "CACHE_CORRUPTED",
                    (
                        "EvidenceSnapshot cannot contain evidence retrieved "
                        "after its trusted purge check."
                    ),
                )
            if observation.source_slot in slots:
                raise FactContractError(
                    "CACHE_CORRUPTED",
                    "EvidenceSnapshot contains duplicate provider LKG slots.",
                )
            slots.add(observation.source_slot)
            retained.append(observation)
        normalized = tuple(
            sorted(
                retained,
                key=lambda item: (
                    item.key.key_id,
                    item.provenance.provider_id,
                    item.observation_id,
                ),
            )
        )
        object.__setattr__(self, "observations", normalized)
        object.__setattr__(self, "evaluation_at", evaluation_at)
        object.__setattr__(self, "purge_checked_at", purge_checked_at)
        expected_revision = _digest(
            {
                "observation_ids": [
                    item.observation_id for item in normalized
                ]
            },
            prefix="active-evidence",
        )
        if self.evidence_revision and self.evidence_revision != expected_revision:
            raise FactContractError(
                "DIGEST_MISMATCH",
                "EvidenceSnapshot.evidence_revision does not match records.",
            )
        object.__setattr__(self, "evidence_revision", expected_revision)
        snapshot_payload = {
            "contract_version": self.contract_version,
            "policy_registry_revision": self.policies.revision,
            "store_revision": self.store_revision,
            "evidence_revision": expected_revision,
            "evaluation_at": _utc_iso(evaluation_at),
            "purge_checked_at": _utc_iso(purge_checked_at),
        }
        if self.outcome_revision is not None:
            snapshot_payload["outcome_revision"] = self.outcome_revision
        expected_snapshot_id = _digest(
            snapshot_payload,
            prefix="evidence-snapshot",
        )
        if self.snapshot_id and self.snapshot_id != expected_snapshot_id:
            raise FactContractError(
                "DIGEST_MISMATCH",
                "EvidenceSnapshot.snapshot_id does not match content.",
            )
        object.__setattr__(self, "snapshot_id", expected_snapshot_id)

    @classmethod
    def from_ledger(
        cls,
        ledger: EvidenceLedger,
        *,
        evaluation_at: datetime,
        purge_now: datetime,
        store_revision: str | None = None,
        outcome_revision: str | None = None,
    ) -> "EvidenceSnapshot":
        """Build a snapshot from one trusted ledger.

        Pure in-memory ledgers use their semantic revision as the source
        revision.  Callers loading a durable EvidenceStore must pass the
        store's exact current revision instead; EvidenceStoreResult.snapshot()
        is the preferred boundary for that case.
        """

        if type(ledger) is not EvidenceLedger:
            raise FactContractError(
                "CACHE_CORRUPTED", "ledger must be exact EvidenceLedger."
            )
        checked_at = _aware_utc(purge_now, "purge_now")
        retained = tuple(
            observation
            for observation in ledger.observations
            if observation.retained_at(checked_at)
        )
        return cls(
            policies=ledger.policies,
            observations=retained,
            evaluation_at=evaluation_at,
            purge_checked_at=checked_at,
            store_revision=(
                ledger.revision
                if store_revision is None
                else store_revision
            ),
            outcome_revision=outcome_revision,
            _token=_SNAPSHOT_TOKEN,
        )

    def resolve(self, key: FactKey) -> FactResolution:
        if type(key) is not FactKey:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST", "key must be exact FactKey."
            )
        visible = tuple(
            observation
            for observation in self.observations
            if observation.key.key_id == key.key_id
            and observation.retrieved_at <= self.evaluation_at
            and observation.retained_at(self.evaluation_at)
        )
        if not visible:
            return FactResolution(
                key=key,
                evidence_state=EvidenceState.UNVERIFIED,
                reason=ResolutionReason.MISSING,
                selected=None,
                candidates=(),
                evaluation_at=self.evaluation_at,
                purge_checked_at=self.purge_checked_at,
                snapshot_id=self.snapshot_id,
                _token=_RESOLUTION_TOKEN,
            )
        ordered = tuple(
            sorted(
                visible,
                key=lambda item: (
                    item.retrieved_at,
                    item.provenance.provider_id,
                    item.observation_id,
                ),
                reverse=True,
            )
        )
        fresh = tuple(
            item for item in ordered if item.fresh_at(self.evaluation_at)
        )
        comparable = fresh or ordered
        value_digests = {
            observation.value.value_digest
            for observation in comparable
        }
        if len(value_digests) > 1:
            return FactResolution(
                key=key,
                evidence_state=EvidenceState.CONFLICTED,
                reason=ResolutionReason.CONFLICTED,
                selected=None,
                candidates=comparable,
                evaluation_at=self.evaluation_at,
                purge_checked_at=self.purge_checked_at,
                snapshot_id=self.snapshot_id,
                _token=_RESOLUTION_TOKEN,
            )
        selected = fresh[0] if fresh else ordered[0]
        state = (
            EvidenceState.VERIFIED if fresh else EvidenceState.STALE
        )
        reason = (
            ResolutionReason.FRESH if fresh else ResolutionReason.STALE
        )
        return FactResolution(
            key=key,
            evidence_state=state,
            reason=reason,
            selected=selected,
            candidates=ordered,
            evaluation_at=self.evaluation_at,
            purge_checked_at=self.purge_checked_at,
            snapshot_id=self.snapshot_id,
            _token=_RESOLUTION_TOKEN,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted snapshot binding, never provider value bytes."""

        result = {
            "contract_version": self.contract_version,
            "policy_registry_revision": self.policies.revision,
            "evaluation_at": _utc_iso(self.evaluation_at),
            "purge_checked_at": _utc_iso(self.purge_checked_at),
            "store_revision": self.store_revision,
            "evidence_revision": self.evidence_revision,
            "snapshot_id": self.snapshot_id,
            "observation_bindings": [
                _observation_binding(
                    observation,
                    policy=self.policies.policy(
                        observation.provenance.retention_policy_id
                    ),
                )
                for observation in self.observations
            ],
        }
        if self.outcome_revision is not None:
            result["outcome_revision"] = self.outcome_revision
        return result


def _validate_result_shape(
    status: ProviderResultStatus,
    observations: tuple[FactObservation, ...],
    problems: tuple[ProviderProblem, ...],
    attempts_used: int,
) -> None:
    if status is ProviderResultStatus.SUCCESS:
        valid = bool(observations) and not problems and attempts_used >= 1
    elif status is ProviderResultStatus.PARTIAL:
        valid = bool(observations) and bool(problems) and attempts_used >= 1
    elif status is ProviderResultStatus.FAILED:
        valid = not observations and bool(problems)
    else:
        valid = bool(observations) and not problems and attempts_used == 0
    if not valid:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            (
                f"ProviderResult status {status.value!r} has an invalid "
                "observation/problem/attempt shape."
            ),
        )


def _observation_binding(
    observation: FactObservation,
    *,
    policy: ProviderPolicy | None = None,
) -> dict[str, Any]:
    provenance = observation.provenance
    static_attributions = (
        tuple(
            (label, None)
            for label in policy.required_attribution_labels
        )
        if policy is not None
        else ()
    )
    return {
        "observation_id": observation.observation_id,
        "key_id": observation.key.key_id,
        "provider_id": provenance.provider_id,
        "adapter_id": provenance.adapter_id,
        "adapter_version": provenance.adapter_version,
        "policy_id": provenance.retention_policy_id,
        "policy_digest": (
            policy.policy_digest if policy is not None else None
        ),
        "persistence": (
            policy.persistence.value if policy is not None else None
        ),
        "valid_until": _utc_iso(observation.valid_until),
        "purge_at": (
            _utc_iso(observation.purge_at)
            if observation.purge_at is not None
            else None
        ),
        "requires_live_attribution": bool(
            provenance.source_uri
            or tuple(provenance.attributions) != static_attributions
        ),
        "required_attribution_labels": (
            list(policy.required_attribution_labels)
            if policy is not None
            else []
        ),
    }


def _validate_key_value_binding(key: FactKey, value: FactValue) -> None:
    payload = value.payload
    qualifiers = key.qualifier_map
    if key.kind in {
        FactKind.PLACE_IDENTITY,
        FactKind.PLACE_PROFILE,
        FactKind.PLACE_OPENING_HOURS,
        FactKind.HOTEL_OFFER,
    } and len(key.subject_ids) != 1:
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            f"{key.kind.value} requires exactly one subject.",
        )
    if key.kind is FactKind.PLACE_PROFILE:
        if qualifiers["provider_place_id"] != payload["provider_place_id"]:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Place profile value differs from its provider identity key.",
            )
    if key.kind is FactKind.PLACE_OPENING_HOURS:
        if qualifiers["provider_place_id"] != payload["provider_place_id"]:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Opening-hours value differs from its provider identity key.",
            )
        if qualifiers["basis"] != payload["basis"]:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Opening-hours basis differs from its fact key.",
            )
        target_start = date.fromisoformat(
            str(qualifiers["target_start"])
        )
        target_end = date.fromisoformat(str(qualifiers["target_end"]))
        coverage_start = date.fromisoformat(payload["coverage_start"])
        coverage_end = date.fromisoformat(payload["coverage_end"])
        if not (
            coverage_start <= target_start
            and target_end <= coverage_end
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Opening-hours coverage does not include the requested dates.",
            )
    if key.kind is FactKind.ROUTE_ESTIMATE:
        if len(key.subject_ids) != 2:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "route_estimate requires ordered origin and destination subjects.",
            )
        if qualifiers.get("mode") != payload["mode"]:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Route key mode differs from normalized route mode.",
            )
        if payload["duration_min"] <= 0:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "A route between distinct subjects requires positive duration.",
            )
        for field in ("departure_at", "arrival_at"):
            if field in qualifiers:
                if field not in payload:
                    raise FactContractError(
                        "EVIDENCE_BINDING_MISMATCH",
                        f"Route value omits requested {field} context.",
                    )
                qualifier_value = qualifiers.get(field)
                if not isinstance(qualifier_value, str) or (
                    _normalize_datetime_text(qualifier_value, field)
                    != payload[field]
                ):
                    raise FactContractError(
                        "EVIDENCE_BINDING_MISMATCH",
                        f"Route key {field} differs from normalized value.",
                    )
        if not (
            {"departure_at", "arrival_at"} & set(qualifiers)
        ) and ({"departure_at", "arrival_at"} & set(payload)):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Untimed route keys cannot accept time-dependent values.",
            )
        if payload["mode"] == "transit" and not (
            "departure_at" in qualifiers or "arrival_at" in qualifiers
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Transit route facts require departure_at or arrival_at context.",
            )


def _validate_key_provenance_binding(
    key: FactKey,
    provenance: ProviderProvenance,
) -> None:
    if key.kind in {
        FactKind.PLACE_IDENTITY,
        FactKind.PLACE_PROFILE,
        FactKind.PLACE_OPENING_HOURS,
    } and key.qualifier_map["identity_provider"] != (
        provenance.provider_id
    ):
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            "Place fact provider scope differs from observation provenance.",
        )


def _validate_observation_time_semantics(
    key: FactKey,
    value: FactValue,
    retrieved_at: datetime,
    valid_until: datetime,
) -> None:
    if key.kind is not FactKind.PLACE_OPENING_HOURS:
        return
    payload = value.payload
    if payload["basis"] != "current":
        return
    _, local_zone = _normalized_timezone(
        payload["timezone"], "timezone"
    )
    retrieved_local_date = retrieved_at.astimezone(local_zone).date()
    coverage_start = date.fromisoformat(payload["coverage_start"])
    coverage_end = date.fromisoformat(payload["coverage_end"])
    if coverage_start != retrieved_local_date:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            (
                "Current opening hours must begin on the provider retrieval "
                "date in the place timezone."
            ),
        )
    coverage_deadline = datetime.combine(
        coverage_end + timedelta(days=1),
        datetime.min.time(),
        tzinfo=local_zone,
    ).astimezone(timezone.utc)
    if valid_until > coverage_deadline:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Current opening hours cannot remain fresh beyond their coverage.",
        )


def _validate_fact_key_scope(
    kind: FactKind,
    subject_ids: tuple[str, ...],
    qualifiers: Mapping[str, Scalar],
) -> None:
    names = set(qualifiers)
    if kind is FactKind.PLACE_IDENTITY:
        required = {"identity_provider"}
        allowed = required
    elif kind is FactKind.PLACE_PROFILE:
        required = {"identity_provider", "provider_place_id"}
        allowed = required
    elif kind is FactKind.PLACE_OPENING_HOURS:
        required = {
            "identity_provider",
            "provider_place_id",
            "basis",
            "target_start",
            "target_end",
        }
        allowed = required
    elif kind is FactKind.ROUTE_ESTIMATE:
        required = {"mode"}
        allowed = required | {
            "departure_at",
            "arrival_at",
            "routing_preference",
            "avoid_tolls",
            "avoid_highways",
            "avoid_ferries",
        }
    else:
        raise FactContractError(
            "UNSUPPORTED_FACT_KIND",
            (
                f"{kind.value!r} query contracts are reserved for a later "
                "provider slice."
            ),
        )
    if names != required and (
        not required.issubset(names) or not names.issubset(allowed)
    ):
        missing = sorted(required - names)
        unknown = sorted(names - allowed)
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            (
                f"{kind.value} has invalid qualifiers "
                f"(missing={missing!r}, unknown={unknown!r})."
            ),
        )
    if kind in {
        FactKind.PLACE_IDENTITY,
        FactKind.PLACE_PROFILE,
        FactKind.PLACE_OPENING_HOURS,
    }:
        if len(subject_ids) != 1:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                f"{kind.value} requires one stable location subject.",
            )
        if not isinstance(qualifiers["identity_provider"], str):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "identity_provider must be a provider machine name.",
            )
        _require_machine_name(
            qualifiers["identity_provider"], "identity_provider"
        )
    if kind in {
        FactKind.PLACE_PROFILE,
        FactKind.PLACE_OPENING_HOURS,
    } and not isinstance(qualifiers["provider_place_id"], str):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "provider_place_id qualifier must be text.",
        )
    if kind is FactKind.PLACE_OPENING_HOURS:
        if qualifiers["basis"] not in {"current", "regular_typical"}:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Opening-hours basis is unsupported.",
            )
        start = date.fromisoformat(str(qualifiers["target_start"]))
        end = date.fromisoformat(str(qualifiers["target_end"]))
        if end < start or (end - start).days > 30:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Opening-hours target must span at most 31 ordered dates.",
            )
    if kind is FactKind.ROUTE_ESTIMATE:
        if len(subject_ids) != 2:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Route facts require ordered origin and destination subjects.",
            )
        mode = qualifiers["mode"]
        time_fields = names & {"departure_at", "arrival_at"}
        if mode == "transit" and len(time_fields) != 1:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Transit route keys require exactly one time context.",
            )
        if mode != "transit" and len(time_fields) > 1:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Route keys cannot request both departure and arrival time.",
            )
        if qualifiers.get("routing_preference") in {
            "traffic_aware",
            "traffic_aware_optimal",
        } and "departure_at" not in qualifiers:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Traffic-aware route keys require departure_at.",
            )
        for field in ("avoid_tolls", "avoid_highways", "avoid_ferries"):
            if field in qualifiers and not isinstance(
                qualifiers[field], bool
            ):
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    f"{field} must be bool.",
                )
        if "routing_preference" in qualifiers and qualifiers[
            "routing_preference"
        ] not in {
            "traffic_aware",
            "traffic_aware_optimal",
            "traffic_unaware",
        }:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "routing_preference is unsupported.",
            )


def _normalize_payload(
    kind: FactKind,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Normalized fact payload must be a JSON object.",
        )
    try:
        copied = dict(payload)
    except (TypeError, ValueError) as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Normalized fact payload is not a valid string-keyed mapping.",
        ) from exc
    if kind is FactKind.PLACE_IDENTITY:
        return _normalize_place_identity(copied)
    if kind is FactKind.PLACE_PROFILE:
        return _normalize_place_profile(copied)
    if kind is FactKind.PLACE_OPENING_HOURS:
        return _normalize_opening_hours(copied)
    if kind is FactKind.ROUTE_ESTIMATE:
        return _normalize_route(copied)
    raise FactContractError(
        "UNSUPPORTED_FACT_KIND", f"Unsupported fact kind {kind!r}."
    )


def _normalize_place_identity(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(payload, required={"provider_place_id"})
    return {
        "provider_place_id": _normalized_provider_identifier(
            payload["provider_place_id"],
            "provider_place_id",
        )
    }


def _normalize_place_profile(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(
        payload,
        required={"provider_place_id", "latitude", "longitude"},
        optional={
            "display_name",
            "timezone",
            "business_status",
        },
    )
    latitude = _finite_number(payload["latitude"], "latitude")
    longitude = _finite_number(payload["longitude"], "longitude")
    if not -90.0 <= latitude <= 90.0:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE", "latitude is out of range."
        )
    if not -180.0 <= longitude <= 180.0:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE", "longitude is out of range."
        )
    result: dict[str, Any] = {
        "provider_place_id": _normalized_provider_identifier(
            payload["provider_place_id"],
            "provider_place_id",
        ),
        "latitude": latitude,
        "longitude": longitude,
    }
    if "display_name" in payload:
        result["display_name"] = _normalized_text(
            payload["display_name"], "display_name", maximum=512
        )
    if "timezone" in payload:
        result["timezone"], _ = _normalized_timezone(
            payload["timezone"], "timezone"
        )
    if "business_status" in payload:
        business_status = payload["business_status"]
        if (
            not isinstance(business_status, str)
            or business_status not in _BUSINESS_STATUSES
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                f"Unsupported business_status {business_status!r}.",
            )
        result["business_status"] = business_status
    return result


def _normalize_opening_hours(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(
        payload,
        required={
            "provider_place_id",
            "timezone",
            "basis",
            "coverage_start",
            "coverage_end",
            "intervals",
            "closed_dates",
        },
    )
    basis = payload["basis"]
    if (
        not isinstance(basis, str)
        or basis not in {"current", "regular_typical"}
    ):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Opening-hours basis must be current or regular_typical.",
        )
    timezone_name, local_zone = _normalized_timezone(
        payload["timezone"], "timezone"
    )
    coverage_start = _normalize_date_text(
        payload["coverage_start"], "coverage_start"
    )
    coverage_end = _normalize_date_text(
        payload["coverage_end"], "coverage_end"
    )
    start_date = date.fromisoformat(coverage_start)
    end_date = date.fromisoformat(coverage_end)
    if end_date < start_date or (end_date - start_date).days > 30:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Opening-hours coverage must be an ordered range of at most 31 days.",
        )
    if basis == "current" and (end_date - start_date).days > 6:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Current opening hours cannot claim more than seven dates.",
        )
    raw_intervals = payload["intervals"]
    if (
        not isinstance(raw_intervals, list)
        or len(raw_intervals) > _MAX_INTERVALS
    ):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"Opening intervals must be a list of at most {_MAX_INTERVALS}.",
        )
    intervals: list[dict[str, str]] = []
    for index, interval in enumerate(raw_intervals):
        if not isinstance(interval, Mapping):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                f"intervals[{index}] must be an object.",
            )
        interval_dict = dict(interval)
        _exact_fields(
            interval_dict,
            required={"start_at", "end_at"},
            path=f"intervals[{index}]",
        )
        starts_at = _normalize_datetime_text(
            interval_dict["start_at"],
            f"intervals[{index}].start_at",
        )
        ends_at = _normalize_datetime_text(
            interval_dict["end_at"],
            f"intervals[{index}].end_at",
        )
        if _parse_datetime(ends_at) <= _parse_datetime(starts_at):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                f"intervals[{index}] must have positive duration.",
            )
        local_start = _parse_datetime(starts_at).astimezone(local_zone)
        local_end = _parse_datetime(ends_at).astimezone(local_zone)
        if not start_date <= local_start.date() <= end_date:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                (
                    f"intervals[{index}] starts outside the declared local "
                    "coverage range."
                ),
            )
        if local_end.date() > end_date + timedelta(days=1):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                (
                    f"intervals[{index}] ends beyond the overnight boundary "
                    "of the declared local coverage range."
                ),
            )
        intervals.append({"start_at": starts_at, "end_at": ends_at})
    intervals.sort(key=lambda item: (item["start_at"], item["end_at"]))
    if len(
        {(item["start_at"], item["end_at"]) for item in intervals}
    ) != len(intervals):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Opening intervals cannot contain duplicates.",
        )
    for previous, current in zip(intervals, intervals[1:]):
        if _parse_datetime(current["start_at"]) < _parse_datetime(
            previous["end_at"]
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Opening intervals cannot overlap.",
            )
    raw_closed_dates = payload["closed_dates"]
    if not isinstance(raw_closed_dates, list):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "closed_dates must be a list.",
        )
    closed_dates = sorted(
        _normalize_date_text(item, "closed_dates item")
        for item in raw_closed_dates
    )
    if len(set(closed_dates)) != len(closed_dates):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "closed_dates cannot contain duplicates.",
        )
    coverage_dates = {
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    }
    parsed_closed_dates = {
        date.fromisoformat(item) for item in closed_dates
    }
    if not parsed_closed_dates.issubset(coverage_dates):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "closed_dates must fall within opening-hours coverage.",
        )
    open_dates: set[date] = set()
    for interval in intervals:
        local_start = _parse_datetime(interval["start_at"]).astimezone(
            local_zone
        )
        local_end = _parse_datetime(interval["end_at"]).astimezone(
            local_zone
        )
        cursor = local_start.date()
        while cursor <= local_end.date():
            day_start = datetime.combine(
                cursor,
                datetime.min.time(),
                tzinfo=local_zone,
            )
            day_end = day_start + timedelta(days=1)
            if local_start < day_end and local_end > day_start:
                if cursor in coverage_dates:
                    open_dates.add(cursor)
            cursor += timedelta(days=1)
    if open_dates.intersection(parsed_closed_dates) or (
        open_dates.union(parsed_closed_dates) != coverage_dates
    ):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            (
                "Every covered local date must be explicitly open or closed, "
                "but never both."
            ),
        )
    return {
        "provider_place_id": _normalized_provider_identifier(
            payload["provider_place_id"],
            "provider_place_id",
        ),
        "timezone": timezone_name,
        "basis": basis,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "intervals": intervals,
        "closed_dates": closed_dates,
    }


def _normalize_route(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(
        payload,
        required={"mode", "duration_min"},
        optional={
            "distance_km",
            "departure_at",
            "arrival_at",
            "static_duration_min",
            "fallback_from_mode",
            "warning_codes",
        },
    )
    mode = payload["mode"]
    if not isinstance(mode, str) or mode not in _TRAVEL_MODES:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE", f"Unsupported route mode {mode!r}."
        )
    result: dict[str, Any] = {
        "mode": mode,
        "duration_min": _non_negative_number(
            payload["duration_min"], "duration_min"
        ),
    }
    for field in ("distance_km", "static_duration_min"):
        if field in payload:
            result[field] = _non_negative_number(payload[field], field)
    for field in ("departure_at", "arrival_at"):
        if field in payload:
            result[field] = _normalize_datetime_text(
                payload[field], field
            )
    if "departure_at" in result and "arrival_at" in result:
        elapsed_minutes = (
            _parse_datetime(result["arrival_at"])
            - _parse_datetime(result["departure_at"])
        ).total_seconds() / 60
        if elapsed_minutes <= 0:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Route arrival_at must be after departure_at.",
            )
        if not math.isclose(
            elapsed_minutes,
            result["duration_min"],
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Route duration disagrees with departure/arrival timestamps.",
            )
    if "fallback_from_mode" in payload:
        fallback = payload["fallback_from_mode"]
        if (
            not isinstance(fallback, str)
            or fallback != "transit"
            or mode != "driving"
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                (
                    "fallback_from_mode only supports an exact "
                    "transit-to-driving fallback."
                ),
            )
        result["fallback_from_mode"] = fallback
    if "warning_codes" in payload:
        warnings = payload["warning_codes"]
        if (
            not isinstance(warnings, list)
            or len(warnings) > _MAX_WARNINGS
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                f"warning_codes must be a list of at most {_MAX_WARNINGS}.",
            )
        normalized_warnings: list[str] = []
        for warning in warnings:
            normalized_warnings.append(
                _normalized_machine_name(warning, "warning code")
            )
        if len(set(normalized_warnings)) != len(normalized_warnings):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "warning_codes cannot contain duplicates.",
            )
        result["warning_codes"] = sorted(normalized_warnings)
    return result


def _exact_fields(
    payload: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    path: str = "payload",
) -> None:
    if any(not isinstance(key, str) for key in payload):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{path} field names must be text.",
        )
    allowed = required | (optional or set())
    missing = required - set(payload)
    unknown = set(payload) - allowed
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"missing={sorted(missing)!r}")
        if unknown:
            parts.append(f"unknown={sorted(unknown)!r}")
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{path} has invalid fields ({', '.join(parts)}).",
        )


def _decode_json_object(payload: bytes) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FactContractError(
                    "INVALID_PROVIDER_RESPONSE",
                    f"Duplicate normalized payload key {key!r}.",
                )
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda value: (_raise_non_finite(value)),
        )
    except FactContractError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Normalized fact payload is not strict UTF-8 JSON.",
        ) from exc
    if not isinstance(decoded, dict):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Normalized fact payload must decode to an object.",
        )
    return decoded


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Normalized fact value is not strict JSON.",
        ) from exc


def _digest(payload: Any, *, prefix: str) -> str:
    encoded = _canonical_json({"prefix": prefix, "payload": payload})
    return hashlib.sha256(encoded).hexdigest()


def _is_sensitive_qualifier(name: str) -> bool:
    parts = set(name.split("_"))
    compact = name.replace("_", "")
    return bool(
        parts & _SENSITIVE_QUALIFIER_PARTS
        or compact
        in {
            "apikey",
            "accesstoken",
            "authorization",
            "credential",
            "password",
            "sig",
            "signature",
            "secret",
            "sessiontoken",
            "token",
        }
    )


def _normalize_named_scalars(
    values: Iterable[tuple[str, Scalar]],
    *,
    path: str,
    secret_code: str,
) -> tuple[tuple[str, Scalar], ...]:
    try:
        items = tuple(values)
    except TypeError as exc:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            f"{path} must be an iterable of named scalar values.",
        ) from exc
    if len(items) > _MAX_QUALIFIERS:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            f"{path} cannot exceed {_MAX_QUALIFIERS} items.",
        )
    normalized: list[tuple[str, Scalar]] = []
    seen: set[str] = set()
    for item in items:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                f"{path} items must be (name, scalar) tuples.",
            )
        name, value = item
        if not _QUALIFIER_RE.fullmatch(name):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                f"{path} contains invalid field name {name!r}.",
            )
        if _is_sensitive_qualifier(name):
            raise FactContractError(
                secret_code,
                f"{path} cannot contain sensitive field {name!r}.",
            )
        if name in seen:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                f"{path} contains duplicate field {name!r}.",
            )
        seen.add(name)
        normalized_value = _normalize_scalar(value, name)
        if isinstance(normalized_value, str):
            _reject_secret_text(
                normalized_value,
                secret_code,
                f"{path}.{name}",
            )
        normalized.append((name, normalized_value))
    return tuple(sorted(normalized))


def _normalize_scalar(value: Any, name: str) -> Scalar:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            return _normalized_text(value, name, maximum=1024)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return 0.0 if value == 0 else value
    raise FactContractError(
        "INVALID_PROVIDER_REQUEST",
        f"FactKey qualifier {name!r} must be a finite JSON scalar.",
    )


def _normalize_qualifier(
    kind: FactKind,
    name: str,
    value: Any,
) -> Scalar:
    try:
        if name in {"departure_at", "arrival_at"}:
            return _normalize_datetime_text(value, name)
        if name in {
            "check_in",
            "check_out",
            "departure_date",
            "return_date",
            "target_date",
            "target_start",
            "target_end",
        }:
            return _normalize_date_text(value, name)
        if name == "mode":
            if not isinstance(value, str) or value not in _TRAVEL_MODES:
                raise FactContractError(
                    "INVALID_PROVIDER_RESPONSE",
                    f"Unsupported route mode {value!r}.",
                )
            return value
        normalized = _normalize_scalar(value, name)
        if isinstance(normalized, str):
            _reject_secret_text(
                normalized,
                "SECRET_IN_PROVIDER_REQUEST",
                f"FactKey qualifier {name!r}",
            )
        return normalized
    except FactContractError as exc:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST", exc.message
        ) from exc


def _normalized_text(value: Any, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE", f"{name} must be text."
        )
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must contain 1 to {maximum} visible characters.",
        )
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} cannot contain control or format characters.",
        )
    return normalized


def _require_visible_text(value: Any, name: str, *, maximum: int) -> None:
    try:
        _normalized_text(value, name, maximum=maximum)
    except FactContractError as exc:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST", exc.message
        ) from exc


def _normalized_machine_name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a lowercase machine name.",
        )
    return value


def _require_machine_name(value: Any, name: str) -> None:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            f"{name} must be a lowercase machine name.",
        )


def _require_digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or not _HEX_DIGEST_RE.fullmatch(value):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a lowercase SHA-256 digest.",
        )


def _normalized_public_uri(value: Any, name: str) -> str:
    normalized = _normalized_text(value, name, maximum=2048)
    _reject_secret_text(
        normalized,
        "INVALID_PROVIDER_RESPONSE",
        name,
    )
    try:
        split = urlsplit(normalized)
        hostname = split.hostname
    except ValueError as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a valid HTTP(S) URI.",
        ) from exc
    if (
        split.scheme not in {"http", "https"}
        or not split.netloc
        or hostname is None
        or split.username is not None
        or split.password is not None
    ):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a credential-free HTTP(S) URI.",
        )
    if split.fragment:
        _reject_secret_text(
            split.fragment,
            "INVALID_PROVIDER_RESPONSE",
            f"{name} fragment",
        )
    for query_name, _query_value in parse_qsl(
        split.query, keep_blank_values=True
    ):
        normalized_name = re.sub(
            r"[^a-z0-9_]+", "_", query_name.casefold()
        ).strip("_")
        if normalized_name and _is_sensitive_qualifier(normalized_name):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                f"{name} cannot contain credential query parameters.",
            )
    return normalized


def _aware_utc(value: Any, name: str) -> datetime:
    try:
        valid = (
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        )
        normalized = value.astimezone(timezone.utc) if valid else None
    except (OverflowError, ValueError) as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a representable timezone-aware datetime.",
        ) from exc
    if not valid or normalized is None:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a timezone-aware datetime.",
        )
    return normalized


def _utc_iso(value: datetime) -> str:
    try:
        normalized = value.astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError) as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Timestamp is outside the supported UTC range.",
        ) from exc
    return normalized.replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize_datetime_text(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not _RFC3339_TIMESTAMP_RE.fullmatch(value)
    ):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be an RFC 3339 timestamp.",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                f"{name} must include a UTC offset.",
            )
        return _utc_iso(parsed)
    except FactContractError:
        raise
    except (OverflowError, ValueError) as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be an RFC 3339 timestamp.",
        ) from exc


def _normalize_date_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be an ISO date.",
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be an ISO date.",
        ) from exc
    if value != parsed.isoformat():
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must use canonical ISO date form.",
        )
    return value


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE", f"{name} must be finite."
        )
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE", f"{name} must be finite."
        ) from exc
    if not math.isfinite(normalized):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE", f"{name} must be finite."
        )
    return 0.0 if normalized == 0 else normalized


def _normalized_timezone(value: Any, name: str) -> tuple[str, ZoneInfo]:
    normalized = _normalized_text(value, name, maximum=128)
    try:
        zone = ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a known IANA timezone.",
        ) from exc
    return normalized, zone


def _normalized_provider_identifier(value: Any, name: str) -> str:
    normalized = _normalized_text(value, name, maximum=512)
    _reject_secret_text(
        normalized,
        "INVALID_PROVIDER_RESPONSE",
        name,
    )
    return normalized


def _reject_secret_text(value: str, code: str, name: str) -> None:
    if _SECRET_VALUE_RE.search(value):
        raise FactContractError(
            code,
            f"{name} cannot contain credential values.",
        )


def _non_negative_number(value: Any, name: str) -> float:
    normalized = _finite_number(value, name)
    if normalized < 0:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be non-negative.",
        )
    return normalized


def _raise_non_finite(value: str) -> None:
    raise FactContractError(
        "INVALID_PROVIDER_RESPONSE",
        f"Non-finite JSON number {value!r} is forbidden.",
    )


__all__ = [
    "AuthorizedProviderResult",
    "EVIDENCE_SNAPSHOT_VERSION",
    "EvidencePersistence",
    "FACT_OBSERVATION_VERSION",
    "FACT_QUERY_VERSION",
    "GOOGLE_MAPS_NON_EEA_POLICY_PROFILE",
    "PROVIDER_RESULT_VERSION",
    "EvidenceLedger",
    "EvidenceMerge",
    "EvidencePrune",
    "EvidenceSnapshot",
    "FactContractError",
    "FactKey",
    "FactKind",
    "FactObservation",
    "FactResolution",
    "FactValue",
    "ProviderProblem",
    "ProviderProblemCode",
    "ProviderPolicy",
    "ProviderPolicyRegistry",
    "ProviderProvenance",
    "ProviderRequest",
    "ProviderResult",
    "ProviderResultStatus",
    "ResolutionReason",
    "authorize_provider_result",
    "google_maps_policy_registry",
    "merge_provider_result",
    "prune_evidence",
    "provider_request_fingerprint",
]
