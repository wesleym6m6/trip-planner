"""Bounded, callback-free projection of one evidence snapshot.

Frozen dataclasses are not a security boundary inside one Python process: a
field can be changed with ``object.__setattr__`` and class methods or slot
descriptors can be monkeypatched.  This module therefore reads exact slotted
instances through import-time-pinned member descriptors, detaches every value
into built-in immutable primitives, validates the detached projection, and
then repeats the read to reject a mixed-generation view.

The returned tuple is an internal data projection, not authority and not a
serialized contract.  Public consumers must build it directly from the raw
``EvidenceSnapshot`` at their own boundary; no consumer accepts a caller-
supplied projection or validation token.  The projector trusts this module
and Python's standard-library implementation, while avoiding callbacks into
the mutable evidence objects and the ``facts`` module after import.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .facts import (
    EVIDENCE_SNAPSHOT_VERSION,
    FACT_OBSERVATION_VERSION,
    FACT_QUERY_VERSION,
    EvidencePersistence,
    EvidenceSnapshot,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderPolicy,
    ProviderPolicyRegistry,
    ProviderProvenance,
)


MAX_EVIDENCE_INTEGRITY_OBSERVATIONS = 4096
MAX_EVIDENCE_INTEGRITY_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_INTEGRITY_POLICIES = 256
MAX_EVIDENCE_INTEGRITY_POLICY_ITEMS = 256

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MACHINE_RE = re.compile(r"[a-z][a-z0-9._/-]{0,127}")
_QUALIFIER_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T"
    r"\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?"
    r"(?:Z|[+-]\d{2}:\d{2})"
)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|credential|password|secret|token)"
    r"\s*['\"]?\s*(?:[:=]|/)\s*['\"]?\s*[^\s'\"/?#&]+"
    r"|(?:bearer|basic)\s+[a-z0-9._~+/=-]{8,}"
)
_SENSITIVE_PARTS = frozenset(
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
_MAX_OPENING_INTERVALS = 128
_MAX_ROUTE_WARNINGS = 16
_UTC = timezone.utc

# Pin standard-library entry points used by the primitive projector.  In
# particular, never call back through mutable helpers in ``facts`` after the
# evidence classes and descriptors above have been imported.
_JSON_LOADS = json.loads
_JSON_DUMPS = json.dumps
_SHA256 = hashlib.sha256
_MATH_ISFINITE = math.isfinite
_MATH_ISCLOSE = math.isclose
_UNICODE_NORMALIZE = unicodedata.normalize
_UNICODE_CATEGORY = unicodedata.category
_URL_SPLIT = urlsplit
_URL_PARSE_QSL = parse_qsl
_RE_SUB = re.sub
_ZONE_INFO = ZoneInfo
_DATE_FROM_ISO = date.fromisoformat
_DATETIME_FROM_ISO = datetime.fromisoformat
_DATETIME_COMBINE = datetime.combine
_MIDNIGHT_TIME = time.min

# Pin enum members and never consult a monkeypatchable Enum ``.value``.
_KIND_OBJECTS = (
    (FactKind.PLACE_IDENTITY, "place_identity"),
    (FactKind.PLACE_PROFILE, "place_profile"),
    (FactKind.PLACE_OPENING_HOURS, "place_opening_hours"),
    (FactKind.ROUTE_ESTIMATE, "route_estimate"),
    (FactKind.FLIGHT_OFFER, "flight_offer"),
    (FactKind.HOTEL_OFFER, "hotel_offer"),
)
_PERSISTENCE_OBJECTS = (
    (EvidencePersistence.DISK_TTL, "disk_ttl"),
    (EvidencePersistence.MEMORY_ONLY, "memory_only"),
    (EvidencePersistence.INDEFINITE_ID, "indefinite_id"),
)
_VALUE_SCHEMAS = {
    "place_identity": "place-identity/v1",
    "place_profile": "place-profile/v1",
    "place_opening_hours": "place-opening-hours/v1",
    "route_estimate": "route-estimate/v1",
}


def _descriptors(cls: type, names: tuple[str, ...]) -> tuple[object, ...]:
    return tuple(cls.__dict__[name] for name in names)


_SNAPSHOT_FIELDS = _descriptors(
    EvidenceSnapshot,
    (
        "policies",
        "observations",
        "evaluation_at",
        "purge_checked_at",
        "store_revision",
        "contract_version",
        "evidence_revision",
        "outcome_revision",
        "snapshot_id",
    ),
)
_REGISTRY_FIELDS = _descriptors(
    ProviderPolicyRegistry,
    ("policies", "revision"),
)
_POLICY_FIELDS = _descriptors(
    ProviderPolicy,
    (
        "policy_id",
        "provider_id",
        "adapter_id",
        "adapter_version",
        "contract_region",
        "allowed_fact_kinds",
        "allowed_value_fields",
        "allowed_operations",
        "persistence",
        "max_validity_seconds",
        "max_retention_seconds",
        "allowed_query_fields",
        "required_attribution_labels",
        "policy_digest",
    ),
)
_OBSERVATION_FIELDS = _descriptors(
    FactObservation,
    (
        "key",
        "value",
        "provenance",
        "retrieved_at",
        "valid_until",
        "purge_at",
        "confidence",
        "contract_version",
        "observation_id",
    ),
)
_KEY_FIELDS = _descriptors(
    FactKey,
    ("kind", "subject_ids", "qualifiers", "contract_version", "key_id"),
)
_VALUE_FIELDS = _descriptors(
    FactValue,
    ("kind", "schema_version", "canonical_json", "value_digest"),
)
_PROVENANCE_FIELDS = _descriptors(
    ProviderProvenance,
    (
        "provider_id",
        "adapter_id",
        "adapter_version",
        "request_fingerprint",
        "retention_policy_id",
        "provider_record_id",
        "response_id",
        "source_uri",
        "attributions",
    ),
)


# Projection tuple shapes.  They remain private implementation details.
# snapshot: contract, policy_revision, store, evidence, outcome, snapshot_id,
#           evaluation, purge_checked, policies, observations
# policy:   id, provider, adapter, version, region, kinds, value_fields,
#           operations, persistence, max_validity, max_retention,
#           query_fields, attribution_labels, digest
# observation: contract, id, key, value, provenance, retrieved, valid, purge,
#              confidence
# key:      kind, subjects, qualifiers, contract, id
# value:    kind, schema, canonical_json, digest
# provenance: provider, adapter, version, request, policy, record, response,
#             uri, attributions


def validate_evidence_snapshot_integrity(snapshot: EvidenceSnapshot) -> tuple:
    """Return a bounded immutable projection or raise ``ValueError``.

    This is deliberately stricter than the general facts constructors: the
    delivery/readiness consumer profile requires exact primitive containers,
    canonical UTC (including ``fold == 0``), and explicit aggregate bounds.
    """

    try:
        first = _read_snapshot_projection(snapshot)
        _validate_snapshot_projection(first)
        second = _read_snapshot_projection(snapshot)
        _validate_snapshot_projection(second)
        if first != second:
            raise ValueError("snapshot changed during integrity projection")
        return first
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        # Missing/deleted slots and all malformed nested values share one
        # stable fail-closed boundary; callers never need to inspect a raw
        # descriptor/parser exception to classify an invalid snapshot.
        raise ValueError("snapshot integrity verification failed") from None


def _read_snapshot_projection(snapshot: object) -> tuple:
    if type(snapshot) is not EvidenceSnapshot:
        raise ValueError("snapshot must be exact")
    (
        policies,
        observations,
        evaluation_at,
        purge_checked_at,
        store_revision,
        contract_version,
        evidence_revision,
        outcome_revision,
        snapshot_id,
    ) = _read_slots(snapshot, EvidenceSnapshot, _SNAPSHOT_FIELDS)
    if type(policies) is not ProviderPolicyRegistry:
        raise ValueError("snapshot policy registry is invalid")
    policy_values, policy_revision = _read_slots(
        policies,
        ProviderPolicyRegistry,
        _REGISTRY_FIELDS,
    )
    if type(policy_values) is not tuple or type(observations) is not tuple:
        raise ValueError("snapshot aggregates must be exact tuples")
    return (
        contract_version,
        policy_revision,
        store_revision,
        evidence_revision,
        outcome_revision,
        snapshot_id,
        evaluation_at,
        purge_checked_at,
        tuple(_read_policy_projection(item) for item in policy_values),
        tuple(_read_observation_projection(item) for item in observations),
    )


def _read_policy_projection(policy: object) -> tuple:
    if type(policy) is not ProviderPolicy:
        raise ValueError("snapshot policy is invalid")
    values = _read_slots(policy, ProviderPolicy, _POLICY_FIELDS)
    kinds = values[5]
    persistence = values[8]
    if type(kinds) is not tuple:
        raise ValueError("snapshot policy kinds must be an exact tuple")
    return (
        *values[:5],
        tuple(_kind_literal(item) for item in kinds),
        *values[6:8],
        _persistence_literal(persistence),
        *values[9:],
    )


def _read_observation_projection(observation: object) -> tuple:
    if type(observation) is not FactObservation:
        raise ValueError("snapshot observation is invalid")
    (
        key,
        value,
        provenance,
        retrieved_at,
        valid_until,
        purge_at,
        confidence,
        contract_version,
        observation_id,
    ) = _read_slots(observation, FactObservation, _OBSERVATION_FIELDS)
    return (
        contract_version,
        observation_id,
        _read_key_projection(key),
        _read_value_projection(value),
        _read_provenance_projection(provenance),
        retrieved_at,
        valid_until,
        purge_at,
        confidence,
    )


def _read_key_projection(key: object) -> tuple:
    if type(key) is not FactKey:
        raise ValueError("snapshot fact key is invalid")
    kind, subjects, qualifiers, contract_version, key_id = _read_slots(
        key,
        FactKey,
        _KEY_FIELDS,
    )
    if type(subjects) is not tuple or type(qualifiers) is not tuple:
        raise ValueError("snapshot fact key aggregates must be exact tuples")
    detached_qualifiers: list[tuple[object, object]] = []
    for item in qualifiers:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("snapshot fact qualifier is invalid")
        detached_qualifiers.append((item[0], item[1]))
    return (
        _kind_literal(kind),
        tuple(subjects),
        tuple(detached_qualifiers),
        contract_version,
        key_id,
    )


def _read_value_projection(value: object) -> tuple:
    if type(value) is not FactValue:
        raise ValueError("snapshot fact value is invalid")
    kind, schema, canonical_json, digest = _read_slots(
        value,
        FactValue,
        _VALUE_FIELDS,
    )
    return (_kind_literal(kind), schema, canonical_json, digest)


def _read_provenance_projection(provenance: object) -> tuple:
    if type(provenance) is not ProviderProvenance:
        raise ValueError("snapshot provenance is invalid")
    values = _read_slots(
        provenance,
        ProviderProvenance,
        _PROVENANCE_FIELDS,
    )
    attributions = values[8]
    if type(attributions) is not tuple:
        raise ValueError("snapshot attribution aggregate is invalid")
    detached: list[tuple[object, object]] = []
    for item in attributions:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("snapshot attribution is invalid")
        detached.append((item[0], item[1]))
    return (*values[:8], tuple(detached))


def _read_slots(
    instance: object,
    owner: type,
    descriptors: tuple[object, ...],
) -> tuple:
    return tuple(
        descriptor.__get__(instance, owner)  # type: ignore[attr-defined]
        for descriptor in descriptors
    )


def _validate_snapshot_projection(snapshot: tuple) -> None:
    (
        contract_version,
        policy_revision,
        store_revision,
        evidence_revision,
        outcome_revision,
        snapshot_id,
        evaluation_at,
        purge_checked_at,
        policies,
        observations,
    ) = snapshot
    if (
        type(contract_version) is not str
        or contract_version != EVIDENCE_SNAPSHOT_VERSION
        or not _digest_text(policy_revision)
        or not _digest_text(store_revision)
        or not _digest_text(evidence_revision)
        or not _digest_text(snapshot_id)
        or (outcome_revision is not None and not _digest_text(outcome_revision))
        or not _exact_factory_utc(evaluation_at)
        or not _exact_factory_utc(purge_checked_at)
        or type(policies) is not tuple
        or not policies
        or len(policies) > MAX_EVIDENCE_INTEGRITY_POLICIES
        or type(observations) is not tuple
        or len(observations) > MAX_EVIDENCE_INTEGRITY_OBSERVATIONS
    ):
        raise ValueError("snapshot aggregate is invalid")

    policy_by_id: dict[str, tuple] = {}
    policy_payloads: list[dict[str, object]] = []
    source_slots: set[tuple[str, str, str, str]] = set()
    for policy in policies:
        payload = _validate_policy_projection(policy)
        policy_id = policy[0]
        if policy_id in policy_by_id:
            raise ValueError("snapshot policy ID repeats")
        policy_by_id[policy_id] = policy
        policy_payloads.append(payload)
        for kind in policy[5]:
            slot = (policy[1], policy[2], policy[3], kind)
            if slot in source_slots:
                raise ValueError("snapshot provider policy slots overlap")
            source_slots.add(slot)
    if tuple(item[0] for item in policies) != tuple(
        sorted(item[0] for item in policies)
    ):
        raise ValueError("snapshot policy order drifted")
    expected_policy_revision = _digest_json(
        policy_payloads,
        prefix="provider-policy-registry",
    )
    if policy_revision != expected_policy_revision:
        raise ValueError("snapshot policy registry drifted")

    evidence_bytes = 0
    observation_slots: set[tuple[str, str]] = set()
    observation_ids: list[str] = []
    observation_order: list[tuple[str, str, str]] = []
    for observation in observations:
        canonical_json, payload = _validate_observation_projection(
            observation,
            policies=policy_by_id,
            purge_checked_at=purge_checked_at,
        )
        evidence_bytes += len(canonical_json)
        if evidence_bytes > MAX_EVIDENCE_INTEGRITY_BYTES:
            raise ValueError("snapshot evidence bytes exceed the bound")
        key_id = observation[2][4]
        provider_id = observation[4][0]
        slot = (key_id, provider_id)
        if slot in observation_slots:
            raise ValueError("snapshot observation source slot repeats")
        observation_slots.add(slot)
        observation_ids.append(observation[1])
        observation_order.append((key_id, provider_id, observation[1]))
        del payload
    if tuple(observation_order) != tuple(sorted(observation_order)):
        raise ValueError("snapshot observation order drifted")
    expected_evidence_revision = _digest_json(
        {"observation_ids": observation_ids},
        prefix="active-evidence",
    )
    if evidence_revision != expected_evidence_revision:
        raise ValueError("snapshot evidence revision drifted")
    snapshot_payload: dict[str, object] = {
        "contract_version": contract_version,
        "policy_registry_revision": expected_policy_revision,
        "store_revision": store_revision,
        "evidence_revision": expected_evidence_revision,
        "evaluation_at": _utc_iso(evaluation_at),
        "purge_checked_at": _utc_iso(purge_checked_at),
    }
    if outcome_revision is not None:
        snapshot_payload["outcome_revision"] = outcome_revision
    expected_snapshot_id = _digest_json(
        snapshot_payload,
        prefix="evidence-snapshot",
    )
    if snapshot_id != expected_snapshot_id:
        raise ValueError("snapshot identity drifted")


def _validate_policy_projection(policy: tuple) -> dict[str, object]:
    (
        policy_id,
        provider_id,
        adapter_id,
        adapter_version,
        contract_region,
        kinds,
        value_fields,
        operations,
        persistence,
        max_validity,
        max_retention,
        query_fields,
        labels,
        policy_digest,
    ) = policy
    if any(
        not _machine_name(item)
        for item in (
            policy_id,
            provider_id,
            adapter_id,
            adapter_version,
            contract_region,
        )
    ) or not _digest_text(policy_digest):
        raise ValueError("snapshot policy text is invalid")
    if (
        type(kinds) is not tuple
        or not kinds
        or len(kinds) > len(_KIND_OBJECTS)
        or any(
            kind not in _VALUE_SCHEMAS
            and kind not in {"flight_offer", "hotel_offer"}
            for kind in kinds
        )
        or tuple(sorted(set(kinds))) != kinds
        or not _field_tuple(value_fields, allow_empty=False)
        or not _machine_tuple(operations, allow_empty=False)
        or not _field_tuple(query_fields)
        or not _normalized_label_tuple(labels)
        or persistence not in {"disk_ttl", "memory_only", "indefinite_id"}
        or type(max_validity) is not int
        or not 0 < max_validity <= 2**63 - 1
        or (
            max_retention is not None
            and (
                type(max_retention) is not int
                or not 0 < max_retention <= 2**63 - 1
            )
        )
    ):
        raise ValueError("snapshot policy shape is invalid")
    if persistence == "indefinite_id":
        if (
            max_retention is not None
            or kinds != ("place_identity",)
            or value_fields != ("provider_place_id",)
        ):
            raise ValueError("snapshot indefinite identity policy is invalid")
    elif max_retention is None:
        raise ValueError("snapshot expiring policy has no retention bound")
    identity_payload = {
        "policy_id": policy_id,
        "provider_id": provider_id,
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "contract_region": contract_region,
        "allowed_fact_kinds": list(kinds),
        "allowed_value_fields": list(value_fields),
        "allowed_operations": list(operations),
        "persistence": persistence,
        "max_validity_seconds": max_validity,
        "max_retention_seconds": max_retention,
        "allowed_query_fields": list(query_fields),
        "required_attribution_labels": list(labels),
    }
    if policy_digest != _digest_json(identity_payload, prefix="provider-policy"):
        raise ValueError("snapshot policy identity drifted")
    return {**identity_payload, "policy_digest": policy_digest}


def _validate_observation_projection(
    observation: tuple,
    *,
    policies: dict[str, tuple],
    purge_checked_at: datetime,
) -> tuple[bytes, dict[str, object]]:
    (
        contract_version,
        observation_id,
        key,
        value,
        provenance,
        retrieved_at,
        valid_until,
        purge_at,
        confidence,
    ) = observation
    if (
        type(contract_version) is not str
        or contract_version != FACT_OBSERVATION_VERSION
        or not _digest_text(observation_id)
        or not _exact_factory_utc(retrieved_at)
        or not _exact_factory_utc(valid_until)
        or (purge_at is not None and not _exact_factory_utc(purge_at))
        or type(confidence) is not float
        or not _MATH_ISFINITE(confidence)
        or not 0.0 <= confidence <= 1.0
        or valid_until <= retrieved_at
        or (purge_at is not None and purge_at <= retrieved_at)
    ):
        raise ValueError("snapshot observation shape is invalid")
    key_payload, qualifiers = _validate_key_projection(key)
    value_payload, normalized_payload = _validate_value_projection(value)
    provenance_payload = _validate_provenance_projection(provenance)
    if key[0] != value[0]:
        raise ValueError("snapshot key and value kinds differ")
    if purge_at is None and key[0] != "place_identity":
        raise ValueError("snapshot non-identity evidence has no retention")
    _validate_key_value_binding(
        kind=key[0],
        subjects=key[1],
        qualifiers=qualifiers,
        payload=normalized_payload,
        retrieved_at=retrieved_at,
        valid_until=valid_until,
    )
    if (
        key[0] in {"place_identity", "place_profile", "place_opening_hours"}
        and qualifiers["identity_provider"] != provenance[0]
    ):
        raise ValueError("snapshot key and provenance providers differ")
    policy = policies.get(provenance[4])
    if policy is None:
        raise ValueError("snapshot observation policy is unavailable")
    _validate_observation_policy(
        observation=observation,
        payload=normalized_payload,
        policy=policy,
    )
    if purge_at is not None and purge_checked_at >= purge_at:
        raise ValueError("snapshot observation retention expired")
    if retrieved_at > purge_checked_at:
        raise ValueError("snapshot observation is from the future")
    identity_payload = {
        "contract_version": contract_version,
        "key": key_payload,
        "value": value_payload,
        "provenance": provenance_payload,
        "retrieved_at": _utc_iso(retrieved_at),
        "valid_until": _utc_iso(valid_until),
        "purge_at": _utc_iso(purge_at) if purge_at is not None else None,
        "confidence": confidence,
    }
    if observation_id != _digest_json(identity_payload, prefix="fact-observation"):
        raise ValueError("snapshot observation identity drifted")
    return value[2], normalized_payload


def _validate_key_projection(key: tuple) -> tuple[dict[str, object], dict[str, object]]:
    kind, subjects, qualifiers, contract_version, key_id = key
    if (
        kind not in _VALUE_SCHEMAS and kind not in {"flight_offer", "hotel_offer"}
        or type(contract_version) is not str
        or contract_version != FACT_QUERY_VERSION
        or not _digest_text(key_id)
        or type(subjects) is not tuple
        or not subjects
        or len(subjects) > 16
    ):
        raise ValueError("snapshot fact key shape is invalid")
    normalized_subjects = tuple(
        _normalized_text(item, maximum=256, secret=True) for item in subjects
    )
    if normalized_subjects != subjects or len(set(subjects)) != len(subjects):
        raise ValueError("snapshot fact key subjects are not normalized")
    if type(qualifiers) is not tuple or len(qualifiers) > 32:
        raise ValueError("snapshot fact qualifiers are invalid")
    normalized_qualifiers: list[tuple[str, object]] = []
    seen: set[str] = set()
    for item in qualifiers:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or _QUALIFIER_RE.fullmatch(item[0]) is None
            or _sensitive_qualifier(item[0])
            or item[0] in seen
        ):
            raise ValueError("snapshot fact qualifier is invalid")
        seen.add(item[0])
        normalized_qualifiers.append(
            (item[0], _normalize_qualifier(item[0], item[1]))
        )
    normalized_tuple = tuple(sorted(normalized_qualifiers))
    if _canonical_json_bytes(normalized_tuple) != _canonical_json_bytes(qualifiers):
        raise ValueError("snapshot fact qualifiers are not normalized")
    qualifier_map = dict(normalized_tuple)
    _validate_key_scope(kind, normalized_subjects, qualifier_map)
    key_payload = {
        "contract_version": contract_version,
        "kind": kind,
        "subject_ids": list(normalized_subjects),
        "qualifiers": [[name, item] for name, item in normalized_tuple],
    }
    if key_id != _digest_json(key_payload, prefix="fact-key"):
        raise ValueError("snapshot fact key identity drifted")
    return (
        {
            "contract_version": contract_version,
            "kind": kind,
            "subject_ids": list(normalized_subjects),
            "qualifiers": {name: item for name, item in normalized_tuple},
            "key_id": key_id,
        },
        qualifier_map,
    )


def _validate_value_projection(
    value: tuple,
) -> tuple[dict[str, object], dict[str, object]]:
    kind, schema, canonical_json, value_digest = value
    if (
        kind not in _VALUE_SCHEMAS
        or type(schema) is not str
        or schema != _VALUE_SCHEMAS[kind]
        or type(canonical_json) is not bytes
        or len(canonical_json) > 32_768
        or not _digest_text(value_digest)
    ):
        raise ValueError("snapshot fact value shape is invalid")
    payload = _decode_exact_json_object(canonical_json)
    normalized = _normalize_payload_projection(kind, payload)
    if (
        type(normalized) is not dict
        or canonical_json != _canonical_json_bytes(normalized)
    ):
        raise ValueError("snapshot fact value is not canonical")
    identity = {
        "kind": kind,
        "schema_version": schema,
        "payload": normalized,
    }
    if value_digest != _digest_json(identity, prefix="fact-value"):
        raise ValueError("snapshot fact value identity drifted")
    return ({**identity, "value_digest": value_digest}, normalized)


def _normalize_payload_projection(
    kind: str,
    payload: dict[str, object],
) -> dict[str, object]:
    """Revalidate one decoded fact value without dispatching into ``facts``."""

    if kind == "place_identity":
        return _normalize_place_identity_projection(payload)
    if kind == "place_profile":
        return _normalize_place_profile_projection(payload)
    if kind == "place_opening_hours":
        return _normalize_opening_hours_projection(payload)
    if kind == "route_estimate":
        return _normalize_route_projection(payload)
    raise ValueError("snapshot fact kind has no supported value schema")


def _normalize_place_identity_projection(
    payload: dict[str, object],
) -> dict[str, object]:
    _require_exact_fields(payload, required={"provider_place_id"})
    return {
        "provider_place_id": _normalized_text(
            payload["provider_place_id"],
            maximum=512,
            secret=True,
        )
    }


def _normalize_place_profile_projection(
    payload: dict[str, object],
) -> dict[str, object]:
    _require_exact_fields(
        payload,
        required={"provider_place_id", "latitude", "longitude"},
        optional={"display_name", "timezone", "business_status"},
    )
    latitude = _finite_number(payload["latitude"])
    longitude = _finite_number(payload["longitude"])
    if not -90.0 <= latitude <= 90.0:
        raise ValueError("snapshot place latitude is out of range")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError("snapshot place longitude is out of range")
    normalized: dict[str, object] = {
        "provider_place_id": _normalized_text(
            payload["provider_place_id"],
            maximum=512,
            secret=True,
        ),
        "latitude": latitude,
        "longitude": longitude,
    }
    if "display_name" in payload:
        normalized["display_name"] = _normalized_text(
            payload["display_name"],
            maximum=512,
            secret=False,
        )
    if "timezone" in payload:
        timezone_name, _zone = _normalized_timezone(payload["timezone"])
        normalized["timezone"] = timezone_name
    if "business_status" in payload:
        status = payload["business_status"]
        if type(status) is not str or status not in _BUSINESS_STATUSES:
            raise ValueError("snapshot place business status is invalid")
        normalized["business_status"] = status
    return normalized


def _normalize_opening_hours_projection(
    payload: dict[str, object],
) -> dict[str, object]:
    _require_exact_fields(
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
    if type(basis) is not str or basis not in {"current", "regular_typical"}:
        raise ValueError("snapshot opening-hours basis is invalid")
    timezone_name, local_zone = _normalized_timezone(payload["timezone"])
    coverage_start = _normalized_date(payload["coverage_start"])
    coverage_end = _normalized_date(payload["coverage_end"])
    start_date = _DATE_FROM_ISO(coverage_start)
    end_date = _DATE_FROM_ISO(coverage_end)
    if end_date < start_date or (end_date - start_date).days > 30:
        raise ValueError("snapshot opening-hours coverage is invalid")
    if basis == "current" and (end_date - start_date).days > 6:
        raise ValueError("snapshot current opening-hours coverage is too long")

    raw_intervals = payload["intervals"]
    if (
        type(raw_intervals) is not list
        or len(raw_intervals) > _MAX_OPENING_INTERVALS
    ):
        raise ValueError("snapshot opening-hours intervals are invalid")
    intervals: list[dict[str, str]] = []
    for interval in raw_intervals:
        if type(interval) is not dict:
            raise ValueError("snapshot opening-hours interval is invalid")
        _require_exact_fields(
            interval,
            required={"start_at", "end_at"},
        )
        starts_at = _normalized_datetime(interval["start_at"])
        ends_at = _normalized_datetime(interval["end_at"])
        parsed_start = _parse_datetime(starts_at)
        parsed_end = _parse_datetime(ends_at)
        if parsed_end <= parsed_start:
            raise ValueError("snapshot opening-hours interval is not positive")
        local_start = parsed_start.astimezone(local_zone)
        local_end = parsed_end.astimezone(local_zone)
        if not start_date <= local_start.date() <= end_date:
            raise ValueError("snapshot opening-hours interval is out of coverage")
        if local_end.date() > local_start.date() + timedelta(days=1):
            raise ValueError("snapshot opening-hours interval is too long")
        if local_end.date() > end_date + timedelta(days=1):
            raise ValueError("snapshot opening-hours interval exceeds coverage")
        intervals.append({"start_at": starts_at, "end_at": ends_at})
    intervals.sort(key=lambda item: (item["start_at"], item["end_at"]))
    if len({(item["start_at"], item["end_at"]) for item in intervals}) != len(
        intervals
    ):
        raise ValueError("snapshot opening-hours intervals repeat")
    for previous, current in zip(intervals, intervals[1:]):
        if _parse_datetime(current["start_at"]) < _parse_datetime(
            previous["end_at"]
        ):
            raise ValueError("snapshot opening-hours intervals overlap")

    raw_closed_dates = payload["closed_dates"]
    if type(raw_closed_dates) is not list:
        raise ValueError("snapshot opening-hours closed dates are invalid")
    closed_dates = sorted(_normalized_date(item) for item in raw_closed_dates)
    if len(set(closed_dates)) != len(closed_dates):
        raise ValueError("snapshot opening-hours closed dates repeat")
    coverage_dates = {
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    }
    parsed_closed_dates = {_DATE_FROM_ISO(item) for item in closed_dates}
    if not parsed_closed_dates.issubset(coverage_dates):
        raise ValueError("snapshot opening-hours closed date is out of coverage")
    open_dates: set[date] = set()
    for interval in intervals:
        local_start = _parse_datetime(interval["start_at"]).astimezone(
            local_zone
        )
        local_end = _parse_datetime(interval["end_at"]).astimezone(local_zone)
        cursor = local_start.date()
        while cursor <= local_end.date():
            day_start = _DATETIME_COMBINE(
                cursor,
                _MIDNIGHT_TIME,
                tzinfo=local_zone,
            )
            day_end = day_start + timedelta(days=1)
            if (
                local_start < day_end
                and local_end > day_start
                and cursor in coverage_dates
            ):
                open_dates.add(cursor)
            cursor += timedelta(days=1)
    if open_dates.intersection(parsed_closed_dates) or (
        open_dates.union(parsed_closed_dates) != coverage_dates
    ):
        raise ValueError("snapshot opening-hours date coverage is incomplete")
    return {
        "provider_place_id": _normalized_text(
            payload["provider_place_id"],
            maximum=512,
            secret=True,
        ),
        "timezone": timezone_name,
        "basis": basis,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "intervals": intervals,
        "closed_dates": closed_dates,
    }


def _normalize_route_projection(
    payload: dict[str, object],
) -> dict[str, object]:
    _require_exact_fields(
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
    if type(mode) is not str or mode not in _TRAVEL_MODES:
        raise ValueError("snapshot route mode is invalid")
    normalized: dict[str, object] = {
        "mode": mode,
        "duration_min": _non_negative_number(payload["duration_min"]),
    }
    for field in ("distance_km", "static_duration_min"):
        if field in payload:
            normalized[field] = _non_negative_number(payload[field])
    for field in ("departure_at", "arrival_at"):
        if field in payload:
            normalized[field] = _normalized_datetime(payload[field])
    if "departure_at" in normalized and "arrival_at" in normalized:
        elapsed_minutes = (
            _parse_datetime(normalized["arrival_at"])
            - _parse_datetime(normalized["departure_at"])
        ).total_seconds() / 60
        if elapsed_minutes <= 0 or not _MATH_ISCLOSE(
            elapsed_minutes,
            normalized["duration_min"],
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError("snapshot route timestamps disagree with duration")
    if "fallback_from_mode" in payload:
        fallback = payload["fallback_from_mode"]
        if type(fallback) is not str or fallback != "transit" or mode != "driving":
            raise ValueError("snapshot route fallback is invalid")
        normalized["fallback_from_mode"] = fallback
    if "warning_codes" in payload:
        warnings = payload["warning_codes"]
        if type(warnings) is not list or len(warnings) > _MAX_ROUTE_WARNINGS:
            raise ValueError("snapshot route warning codes are invalid")
        normalized_warnings = []
        for warning in warnings:
            if not _machine_name(warning):
                raise ValueError("snapshot route warning code is invalid")
            normalized_warnings.append(warning)
        if len(set(normalized_warnings)) != len(normalized_warnings):
            raise ValueError("snapshot route warning codes repeat")
        normalized["warning_codes"] = sorted(normalized_warnings)
    return normalized


def _require_exact_fields(
    payload: dict[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    if type(payload) is not dict or any(type(key) is not str for key in payload):
        raise ValueError("snapshot fact payload fields are invalid")
    if set(payload) != required | (set(payload) & (optional or set())):
        raise ValueError("snapshot fact payload has missing or unknown fields")
    if not required.issubset(payload):
        raise ValueError("snapshot fact payload has missing fields")


def _finite_number(value: object) -> float:
    if type(value) not in {int, float}:
        raise ValueError("snapshot fact number is invalid")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("snapshot fact number is invalid") from None
    if not _MATH_ISFINITE(normalized):
        raise ValueError("snapshot fact number is not finite")
    return 0.0 if normalized == 0 else normalized


def _non_negative_number(value: object) -> float:
    normalized = _finite_number(value)
    if normalized < 0:
        raise ValueError("snapshot fact number is negative")
    return normalized


def _normalized_timezone(value: object) -> tuple[str, ZoneInfo]:
    normalized = _normalized_text(value, maximum=128, secret=False)
    try:
        zone = _ZONE_INFO(normalized)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("snapshot fact timezone is invalid") from None
    return normalized, zone


def _normalized_date(value: object) -> str:
    if type(value) is not str:
        raise ValueError("snapshot fact date is invalid")
    try:
        parsed = _DATE_FROM_ISO(value)
    except ValueError:
        raise ValueError("snapshot fact date is invalid") from None
    if parsed.isoformat() != value:
        raise ValueError("snapshot fact date is not canonical")
    return value


def _normalized_datetime(value: object) -> str:
    if type(value) is not str or _RFC3339_RE.fullmatch(value) is None:
        raise ValueError("snapshot fact timestamp is invalid")
    try:
        parsed = _DATETIME_FROM_ISO(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("snapshot fact timestamp has no offset")
        return _utc_iso(parsed)
    except (OverflowError, ValueError):
        raise ValueError("snapshot fact timestamp is invalid") from None


def _parse_datetime(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("snapshot fact timestamp is invalid")
    try:
        return _DATETIME_FROM_ISO(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        raise ValueError("snapshot fact timestamp is invalid") from None


def _validate_provenance_projection(provenance: tuple) -> dict[str, object]:
    (
        provider_id,
        adapter_id,
        adapter_version,
        request_fingerprint,
        policy_id,
        provider_record_id,
        response_id,
        source_uri,
        attributions,
    ) = provenance
    if any(
        not _machine_name(item)
        for item in (provider_id, adapter_id, adapter_version, policy_id)
    ) or not _digest_text(request_fingerprint):
        raise ValueError("snapshot provenance source is invalid")
    for item in (provider_record_id, response_id):
        if (
            item is not None
            and _normalized_text(item, maximum=512, secret=True) != item
        ):
            raise ValueError("snapshot provenance record is not normalized")
    if source_uri is not None:
        if type(source_uri) is not str:
            raise ValueError("snapshot provenance URI is invalid")
        try:
            normalized_uri = _normalized_public_uri(source_uri)
        except Exception:
            raise ValueError("snapshot provenance URI is invalid") from None
        if normalized_uri != source_uri:
            raise ValueError("snapshot provenance URI is not normalized")
    if type(attributions) is not tuple or len(attributions) > 32:
        raise ValueError("snapshot attribution aggregate is invalid")
    normalized_attributions: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for item in attributions:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or (item[1] is not None and type(item[1]) is not str)
        ):
            raise ValueError("snapshot attribution is invalid")
        label = _normalized_text(item[0], maximum=256, secret=True)
        uri = item[1]
        if uri is not None:
            try:
                uri = _normalized_public_uri(uri)
            except Exception:
                raise ValueError("snapshot attribution URI is invalid") from None
        normalized = (label, uri)
        if normalized not in seen:
            seen.add(normalized)
            normalized_attributions.append(normalized)
    expected = tuple(
        sorted(
            normalized_attributions,
            key=lambda item: (item[0], item[1] or ""),
        )
    )
    if expected != attributions:
        raise ValueError("snapshot attributions are not normalized")
    return {
        "provider_id": provider_id,
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "request_fingerprint": request_fingerprint,
        "retention_policy_id": policy_id,
        "provider_record_id": provider_record_id,
        "response_id": response_id,
        "source_uri": source_uri,
        "attributions": [
            {"label": label, "uri": uri} for label, uri in expected
        ],
    }


def _validate_observation_policy(
    *, observation: tuple, payload: dict[str, object], policy: tuple
) -> None:
    key = observation[2]
    provenance = observation[4]
    retrieved_at = observation[5]
    valid_until = observation[6]
    purge_at = observation[7]
    if (
        (provenance[0], provenance[1], provenance[2])
        != (policy[1], policy[2], policy[3])
        or key[0] not in policy[5]
        or not set(payload).issubset(policy[6])
        or (valid_until - retrieved_at).total_seconds() > policy[9]
    ):
        raise ValueError("snapshot observation is outside policy")
    if policy[8] == "indefinite_id":
        provider_place_id = payload.get("provider_place_id")
        if (
            purge_at is not None
            or provenance[6] is not None
            or provenance[7] is not None
            or provenance[5] not in {None, provider_place_id}
            or provenance[8] != tuple((label, None) for label in policy[12])
        ):
            raise ValueError("snapshot indefinite identity exceeds policy")
    else:
        if (
            purge_at is None
            or policy[10] is None
            or (purge_at - retrieved_at).total_seconds() > policy[10]
        ):
            raise ValueError("snapshot observation retention exceeds policy")
    if not set(policy[12]).issubset(label for label, _uri in provenance[8]):
        raise ValueError("snapshot observation attribution is incomplete")


def _validate_key_value_binding(
    *,
    kind: str,
    subjects: tuple[str, ...],
    qualifiers: dict[str, object],
    payload: dict[str, object],
    retrieved_at: datetime,
    valid_until: datetime,
) -> None:
    if (
        kind in {"place_identity", "place_profile", "place_opening_hours"}
        and len(subjects) != 1
    ):
        raise ValueError("snapshot place key requires one subject")
    if (
        kind in {"place_profile", "place_opening_hours"}
        and qualifiers["provider_place_id"] != payload["provider_place_id"]
    ):
        raise ValueError("snapshot place value differs from its key")
    if kind == "place_opening_hours":
        if qualifiers["basis"] != payload["basis"]:
            raise ValueError("snapshot opening-hours basis differs")
        target_start = _DATE_FROM_ISO(str(qualifiers["target_start"]))
        target_end = _DATE_FROM_ISO(str(qualifiers["target_end"]))
        coverage_start = _DATE_FROM_ISO(str(payload["coverage_start"]))
        coverage_end = _DATE_FROM_ISO(str(payload["coverage_end"]))
        if not (coverage_start <= target_start and target_end <= coverage_end):
            raise ValueError("snapshot opening-hours coverage is incomplete")
        if payload["basis"] == "current":
            zone = _ZONE_INFO(str(payload["timezone"]))
            if coverage_start != retrieved_at.astimezone(zone).date():
                raise ValueError("snapshot current hours start date differs")
            deadline = _DATETIME_COMBINE(
                coverage_end + timedelta(days=1),
                _MIDNIGHT_TIME,
                tzinfo=zone,
            ).astimezone(_UTC)
            if valid_until > deadline:
                raise ValueError(
                    "snapshot current hours validity exceeds coverage"
                )
    if kind == "route_estimate":
        if (
            len(subjects) != 2
            or qualifiers.get("mode") != payload["mode"]
            or float(payload["duration_min"]) <= 0
        ):
            raise ValueError("snapshot route value differs from its key")
        for field in ("departure_at", "arrival_at"):
            if field in qualifiers and payload.get(field) != qualifiers[field]:
                raise ValueError("snapshot route time differs from its key")
        time_fields = {"departure_at", "arrival_at"}
        if not (time_fields & set(qualifiers)) and time_fields & set(payload):
            raise ValueError("snapshot untimed route has timed value")
        if payload["mode"] == "transit" and not (
            time_fields & set(qualifiers)
        ):
            raise ValueError("snapshot transit route has no time context")


def _validate_key_scope(
    kind: str,
    subjects: tuple[str, ...],
    qualifiers: dict[str, object],
) -> None:
    names = set(qualifiers)
    if kind == "place_identity":
        required = {"identity_provider"}
        allowed = required
    elif kind == "place_profile":
        required = {"identity_provider", "provider_place_id"}
        allowed = required
    elif kind == "place_opening_hours":
        required = {
            "identity_provider",
            "provider_place_id",
            "basis",
            "target_start",
            "target_end",
        }
        allowed = required
    elif kind == "route_estimate":
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
        raise ValueError("snapshot fact kind is unsupported")
    if not required.issubset(names) or not names.issubset(allowed):
        raise ValueError("snapshot fact qualifier scope is invalid")
    if kind.startswith("place_"):
        if len(subjects) != 1 or not _machine_name(
            qualifiers["identity_provider"]
        ):
            raise ValueError("snapshot place fact scope is invalid")
    if kind == "place_opening_hours":
        if qualifiers["basis"] not in {"current", "regular_typical"}:
            raise ValueError("snapshot opening-hours basis is invalid")
        start = _DATE_FROM_ISO(str(qualifiers["target_start"]))
        end = _DATE_FROM_ISO(str(qualifiers["target_end"]))
        if end < start or (end - start).days > 30:
            raise ValueError("snapshot opening-hours target is invalid")
    if kind == "route_estimate":
        if len(subjects) != 2 or qualifiers["mode"] not in _TRAVEL_MODES:
            raise ValueError("snapshot route scope is invalid")
        time_fields = names & {"departure_at", "arrival_at"}
        if (
            qualifiers["mode"] == "transit"
            and len(time_fields) != 1
        ) or (
            qualifiers["mode"] != "transit"
            and len(time_fields) > 1
        ):
            raise ValueError("snapshot route time scope is invalid")
        if (
            qualifiers.get("routing_preference")
            in {"traffic_aware", "traffic_aware_optimal"}
            and "departure_at" not in names
        ):
            raise ValueError("snapshot traffic route lacks departure time")
        if any(
            field in qualifiers and type(qualifiers[field]) is not bool
            for field in ("avoid_tolls", "avoid_highways", "avoid_ferries")
        ):
            raise ValueError("snapshot route avoidance scope is invalid")
        if (
            "routing_preference" in qualifiers
            and qualifiers["routing_preference"]
            not in {
                "traffic_aware",
                "traffic_aware_optimal",
                "traffic_unaware",
            }
        ):
            raise ValueError("snapshot routing preference is invalid")


def _normalize_qualifier(name: str, value: object) -> object:
    if name in {"departure_at", "arrival_at"}:
        if type(value) is not str or _RFC3339_RE.fullmatch(value) is None:
            raise ValueError("snapshot route timestamp is invalid")
        parsed = _DATETIME_FROM_ISO(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("snapshot route timestamp lacks an offset")
        return _utc_iso(parsed)
    if name in {
        "check_in",
        "check_out",
        "departure_date",
        "return_date",
        "target_date",
        "target_start",
        "target_end",
    }:
        if type(value) is not str or _DATE_FROM_ISO(value).isoformat() != value:
            raise ValueError("snapshot date qualifier is invalid")
        return value
    if name == "mode":
        if type(value) is not str or value not in _TRAVEL_MODES:
            raise ValueError("snapshot route mode is invalid")
        return value
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        return _normalized_text(value, maximum=1024, secret=True)
    if type(value) is int:
        if not -(2**63) <= value <= 2**63 - 1:
            raise ValueError("snapshot integer qualifier exceeds the bound")
        return value
    if type(value) is float and _MATH_ISFINITE(value):
        return 0.0 if value == 0 else value
    raise ValueError("snapshot fact qualifier is not an exact scalar")


def _decode_exact_json_object(payload: bytes) -> dict[str, object]:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate normalized payload key")
            result[key] = value
        return result
    try:
        decoded = _JSON_LOADS(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_raise_value_error()),
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("snapshot fact value is not strict JSON") from None
    if type(decoded) is not dict:
        raise ValueError("snapshot fact value must be an object")
    return decoded


def _kind_literal(value: object) -> str:
    if type(value) is not FactKind:
        raise ValueError("snapshot fact kind is invalid")
    for expected, literal in _KIND_OBJECTS:
        if value is expected:
            return literal
    raise ValueError("snapshot fact kind is unknown")


def _persistence_literal(value: object) -> str:
    if type(value) is not EvidencePersistence:
        raise ValueError("snapshot evidence persistence is invalid")
    for expected, literal in _PERSISTENCE_OBJECTS:
        if value is expected:
            return literal
    raise ValueError("snapshot evidence persistence is unknown")


def _field_tuple(value: object, *, allow_empty: bool = True) -> bool:
    return (
        type(value) is tuple
        and len(value) <= MAX_EVIDENCE_INTEGRITY_POLICY_ITEMS
        and (allow_empty or bool(value))
        and all(
            type(item) is str and _QUALIFIER_RE.fullmatch(item) is not None
            for item in value
        )
        and tuple(sorted(set(value))) == value
    )


def _machine_tuple(value: object, *, allow_empty: bool = True) -> bool:
    return (
        type(value) is tuple
        and len(value) <= MAX_EVIDENCE_INTEGRITY_POLICY_ITEMS
        and (allow_empty or bool(value))
        and all(_machine_name(item) for item in value)
        and tuple(sorted(set(value))) == value
    )


def _normalized_label_tuple(value: object) -> bool:
    if (
        type(value) is not tuple
        or len(value) > MAX_EVIDENCE_INTEGRITY_POLICY_ITEMS
    ):
        return False
    try:
        normalized = tuple(
            _normalized_text(item, maximum=256, secret=True)
            for item in value
        )
    except ValueError:
        return False
    return normalized == value and tuple(sorted(set(value))) == value


def _normalized_text(value: object, *, maximum: int, secret: bool) -> str:
    if type(value) is not str:
        raise ValueError("snapshot text is not exact")
    normalized = _UNICODE_NORMALIZE("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(
            _UNICODE_CATEGORY(character).startswith("C")
            for character in normalized
        )
        or (secret and _SECRET_RE.search(normalized) is not None)
    ):
        raise ValueError("snapshot text is not normalized")
    return normalized


def _normalized_public_uri(value: object) -> str:
    normalized = _normalized_text(value, maximum=2048, secret=True)
    try:
        split = _URL_SPLIT(normalized)
        hostname = split.hostname
    except (TypeError, ValueError):
        raise ValueError("snapshot provenance URI is invalid") from None
    if (
        split.scheme not in {"http", "https"}
        or not split.netloc
        or hostname is None
        or split.username is not None
        or split.password is not None
    ):
        raise ValueError("snapshot provenance URI is not public HTTP(S)")
    try:
        query_pairs = _URL_PARSE_QSL(split.query, keep_blank_values=True)
    except ValueError:
        raise ValueError("snapshot provenance URI query is invalid") from None
    for query_name, _query_value in query_pairs:
        normalized_name = _RE_SUB(
            r"[^a-z0-9_]+",
            "_",
            query_name.casefold(),
        ).strip("_")
        if normalized_name and _sensitive_qualifier(normalized_name):
            raise ValueError("snapshot provenance URI contains a secret field")
    return normalized


def _machine_name(value: object) -> bool:
    return type(value) is str and _MACHINE_RE.fullmatch(value) is not None


def _sensitive_qualifier(name: str) -> bool:
    parts = set(name.split("_"))
    compact = name.replace("_", "")
    return bool(
        parts & _SENSITIVE_PARTS
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


def _exact_factory_utc(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is _UTC and value.fold == 0


def _digest_text(value: object) -> bool:
    return type(value) is str and _DIGEST_RE.fullmatch(value) is not None


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return _JSON_DUMPS(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ValueError("integrity payload is not canonical JSON") from None


def _digest_json(value: object, *, prefix: str) -> str:
    encoded = _canonical_json_bytes({"prefix": prefix, "payload": value})
    return _SHA256(encoded).hexdigest()


def _utc_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _raise_value_error() -> None:
    raise ValueError("non-finite JSON number")


__all__ = ["validate_evidence_snapshot_integrity"]
