"""Read-only migration previews from legacy trip files to ``plan.json``.

Migration deliberately has no apply/store function.  A preview reads the two
legacy source files, assigns missing stable IDs deterministically, validates a
canonical candidate, and returns the exact candidate bytes a separate atomic
store may later commit after rechecking ``source_revision``.

All migrated activities are protected in revisioned metadata.  Legacy data has
no reliable fixed/booked/flexibility classification, so a later semantic
writer must require human approval before destructively changing those
activities.  The migration does not materialize runtime fallbacks such as UTC,
nor defaults for decision, flexibility, evidence, or duration.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .codec import (
    MIGRATION_META_KEY,
    FrozenJsonValue,
    PlanCodecError,
    build_plan,
    canonical_json_bytes,
    decode_json_bytes,
    decode_plan,
    deep_copy_json,
    encode_plan,
    freeze_json,
    plan_to_trip_state,
)
from .loaders import LoadError


_MIGRATION_NAMESPACE = uuid.UUID("8eb7cd09-af5c-4d21-8e11-804752f89c71")
_SOURCE_REVISION_SEPARATOR = b"\0itinerary\0"
_PREVIEW_DIGEST_DOMAIN = b"trip-planner.migration-preview/v1\0"


class MigrationError(PlanCodecError):
    """A machine-readable migration preview failure."""


@dataclass(frozen=True, slots=True)
class IdAssignment:
    """One legacy source location and its persisted stable ID."""

    entity_kind: str
    source_path: str
    stable_id: str
    preserved: bool

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "entity_kind": self.entity_kind,
            "source_path": self.source_path,
            "stable_id": self.stable_id,
            "preserved": self.preserved,
        }


@dataclass(frozen=True, slots=True)
class MigrationIssue:
    """A deterministic, typed warning or information item from preview."""

    code: str
    severity: str
    message: str
    source_path: str
    details: tuple[tuple[str, str | int | float | bool | None], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "source_path": self.source_path,
            "details": {key: value for key, value in self.details},
        }


@dataclass(frozen=True, slots=True)
class LegacyDocuments:
    """Detached immutable snapshot of the exact legacy migration source."""

    data_dir: Path
    trip_path: Path
    itinerary_path: Path
    trip_bytes: bytes
    itinerary_bytes: bytes
    source_revision: str
    source_file_hashes: tuple[tuple[str, str], ...]
    trip: FrozenJsonValue
    itinerary: FrozenJsonValue

    def mutable_trip(self) -> dict[str, Any]:
        value = deep_copy_json(self.trip)
        assert isinstance(value, dict)
        return value

    def mutable_itinerary(self) -> dict[str, Any]:
        value = deep_copy_json(self.itinerary)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True, slots=True)
class MigrationPreview:
    """Complete immutable handoff to a later atomic migration store."""

    data_dir: Path
    target_path: Path
    source_revision: str
    source_file_hashes: tuple[tuple[str, str], ...]
    candidate_revision: str
    preview_digest: str
    candidate_bytes: bytes
    candidate_plan: FrozenJsonValue
    id_assignments: tuple[IdAssignment, ...]
    issues: tuple[MigrationIssue, ...]
    protected_activity_ids: tuple[str, ...]

    @property
    def id_mapping(self) -> Mapping[str, str]:
        """Read-only JSON-path-to-ID mapping for review UIs."""

        return MappingProxyType(
            {
                assignment.source_path: assignment.stable_id
                for assignment in self.id_assignments
            }
        )

    def mutable_candidate_plan(self) -> dict[str, Any]:
        """Return a detached validated candidate document."""

        value = deep_copy_json(self.candidate_plan)
        assert isinstance(value, dict)
        return value

    def verify_candidate_bytes(self) -> None:
        """Revalidate that the carried bytes match all preview claims."""

        plan = decode_plan(self.candidate_bytes)
        if plan["revision"] != self.candidate_revision:
            raise MigrationError(
                "PREVIEW_MISMATCH",
                "candidate bytes do not match candidate_revision",
                path="$.candidate_revision",
            )
        expected_digest = _preview_digest(
            self.source_revision,
            self.candidate_bytes,
            self.id_assignments,
            self.issues,
        )
        if expected_digest != self.preview_digest:
            raise MigrationError(
                "PREVIEW_MISMATCH",
                "candidate bytes or preview metadata do not match preview_digest",
                path="$.preview_digest",
            )

    def to_dict(self, *, include_candidate: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "data_dir": str(self.data_dir),
            "target_path": str(self.target_path),
            "source_revision": self.source_revision,
            "source_file_hashes": dict(self.source_file_hashes),
            "candidate_revision": self.candidate_revision,
            "candidate_sha256": hashlib.sha256(
                self.candidate_bytes
            ).hexdigest(),
            "preview_digest": self.preview_digest,
            "id_assignments": [
                assignment.to_dict() for assignment in self.id_assignments
            ],
            "issues": [issue.to_dict() for issue in self.issues],
            "protected_activity_ids": list(self.protected_activity_ids),
        }
        if include_candidate:
            result["candidate"] = self.mutable_candidate_plan()
        return result


def compute_legacy_source_revision(
    trip_bytes: bytes, itinerary_bytes: bytes
) -> str:
    """Return the exact source revision used by the existing legacy loader."""

    return hashlib.sha256(
        trip_bytes + _SOURCE_REVISION_SEPARATOR + itinerary_bytes
    ).hexdigest()


def read_legacy_documents(path: str | Path) -> LegacyDocuments:
    """Read a strict, immutable legacy source snapshot without writing."""

    data_dir = _find_data_dir(Path(path))
    trip_path = data_dir / "trip.json"
    itinerary_path = data_dir / "itinerary.json"
    try:
        trip_bytes = trip_path.read_bytes()
        itinerary_bytes = itinerary_path.read_bytes()
    except OSError as exc:
        raise MigrationError(
            "READ_ERROR",
            str(exc),
            path=str(getattr(exc, "filename", None) or data_dir),
        ) from exc

    trip_value = decode_json_bytes(trip_bytes)
    itinerary_value = decode_json_bytes(itinerary_bytes)
    if not isinstance(trip_value, dict):
        raise MigrationError(
            "MALFORMED_LEGACY",
            "trip.json root must be an object",
            path=f"{trip_path}:$",
        )
    if not isinstance(itinerary_value, dict):
        raise MigrationError(
            "MALFORMED_LEGACY",
            "itinerary.json root must be an object",
            path=f"{itinerary_path}:$",
        )

    source_revision = compute_legacy_source_revision(
        trip_bytes, itinerary_bytes
    )
    hashes = (
        ("trip.json", hashlib.sha256(trip_bytes).hexdigest()),
        ("itinerary.json", hashlib.sha256(itinerary_bytes).hexdigest()),
    )
    return LegacyDocuments(
        data_dir=data_dir,
        trip_path=trip_path,
        itinerary_path=itinerary_path,
        trip_bytes=trip_bytes,
        itinerary_bytes=itinerary_bytes,
        source_revision=source_revision,
        source_file_hashes=hashes,
        trip=freeze_json(trip_value),
        itinerary=freeze_json(itinerary_value),
    )


def legacy_source_revision(path: str | Path) -> str:
    """Read and return the current exact legacy source revision."""

    return read_legacy_documents(path).source_revision


def preview_legacy_migration(
    path: str | Path,
    *,
    generation: int = 1,
) -> MigrationPreview:
    """Build a deterministic, read-only canonical migration preview."""

    source = read_legacy_documents(path)
    target_path = source.data_dir / "plan.json"
    if target_path.exists() or target_path.is_symlink():
        raise MigrationError(
            "PLAN_ALREADY_EXISTS",
            "canonical plan.json already exists; legacy migration will not overwrite it",
            path=str(target_path),
        )

    trip = source.mutable_trip()
    itinerary = source.mutable_itinerary()
    if MIGRATION_META_KEY in trip:
        raise MigrationError(
            "RESERVED_FIELD",
            f"legacy trip.json already contains reserved field {MIGRATION_META_KEY!r}",
            path=f"{source.trip_path}:$.{MIGRATION_META_KEY}",
        )

    assignments: list[IdAssignment] = []
    issues: list[MigrationIssue] = []

    slug_value = trip.get("slug")
    if slug_value is None:
        slug = _resolved_fallback_slug(source.data_dir)
    else:
        slug = _require_text(
            slug_value, f"{source.trip_path}:$.slug", code="MALFORMED_LEGACY"
        )
    if not slug:
        raise MigrationError(
            "MALFORMED_LEGACY",
            "trip slug is required to assign a deterministic trip ID",
            path=f"{source.trip_path}:$.slug",
        )

    trip_id, preserved_trip_id = _existing_or_generated_id(
        trip,
        primary="trip_id",
        alias=None,
        generated=_stable_id("trip", slug),
        path=f"{source.trip_path}:$",
    )
    assignments.append(
        IdAssignment(
            entity_kind="trip",
            source_path=f"{source.trip_path}:$.trip_id",
            stable_id=trip_id,
            preserved=preserved_trip_id,
        )
    )

    raw_days = itinerary.get("days")
    days = _require_list(
        raw_days,
        f"{source.itinerary_path}:$.days",
        nonempty=True,
    )
    seen_day_ids: set[str] = set()
    seen_activity_ids: set[str] = set()
    seen_constraint_ids: set[str] = set()
    protected_activity_ids: list[str] = []
    ignored_travel_edges: list[dict[str, Any]] = []

    for day_index, day_value in enumerate(days):
        day_path = f"{source.itinerary_path}:$.days[{day_index}]"
        day = _require_object(day_value, day_path)
        day_id, preserved = _existing_or_generated_id(
            day,
            primary="day_id",
            alias="id",
            generated=_stable_id("day", trip_id, str(day_index)),
            path=day_path,
        )
        _record_unique(day_id, seen_day_ids, "day", f"{day_path}.day_id")
        day["day_id"] = day_id
        assignments.append(
            IdAssignment(
                entity_kind="day",
                source_path=f"{day_path}.day_id",
                stable_id=day_id,
                preserved=preserved,
            )
        )

        places = _require_list(
            day.get("places"),
            f"{day_path}.places",
            nonempty=False,
        )
        ordered_activity_ids: list[str] = []
        for activity_index, activity_value in enumerate(places):
            activity_path = f"{day_path}.places[{activity_index}]"
            activity = _require_object(activity_value, activity_path)
            activity_id, preserved_activity = _existing_or_generated_id(
                activity,
                primary="activity_id",
                alias="id",
                generated=_stable_id(
                    "activity",
                    trip_id,
                    day_id,
                    str(activity_index),
                ),
                path=activity_path,
            )
            _record_unique(
                activity_id,
                seen_activity_ids,
                "activity",
                f"{activity_path}.activity_id",
            )
            activity["activity_id"] = activity_id
            ordered_activity_ids.append(activity_id)
            protected_activity_ids.append(activity_id)
            assignments.append(
                IdAssignment(
                    entity_kind="activity",
                    source_path=f"{activity_path}.activity_id",
                    stable_id=activity_id,
                    preserved=preserved_activity,
                )
            )

            location_id, preserved_location = _location_id(
                activity,
                trip_id=trip_id,
                source_path=activity_path,
            )
            activity["location_id"] = location_id
            assignments.append(
                IdAssignment(
                    entity_kind="location",
                    source_path=f"{activity_path}.location_id",
                    stable_id=location_id,
                    preserved=preserved_location,
                )
            )

        original_travel = _require_list(
            day.get("travel", []),
            f"{day_path}.travel",
            nonempty=False,
        )
        canonical_travel: list[dict[str, Any]] = []
        activity_index_by_id = {
            activity_id: index
            for index, activity_id in enumerate(ordered_activity_ids)
        }
        for edge_index, edge_value in enumerate(original_travel):
            edge_path = f"{day_path}.travel[{edge_index}]"
            edge = _require_object(edge_value, edge_path)
            migrated_edge = deep_copy_json(edge)
            assert isinstance(migrated_edge, dict)
            outcome = _add_travel_endpoint_ids(
                migrated_edge,
                ordered_activity_ids=ordered_activity_ids,
                activity_index_by_id=activity_index_by_id,
                source_path=edge_path,
            )
            if outcome is None:
                ignored_travel_edges.append(
                    {
                        "day_id": day_id,
                        "index": edge_index,
                        "edge": deep_copy_json(edge),
                    }
                )
                issues.append(
                    MigrationIssue(
                        code="INVALID_TRAVEL_REFERENCE",
                        severity="warning",
                        message=(
                            "Moved a derived travel edge with out-of-range "
                            "legacy indices out of canonical state."
                        ),
                        source_path=edge_path,
                        details=(
                            ("activity_count", len(ordered_activity_ids)),
                            ("from", _detail_scalar(edge.get("from"))),
                            ("to", _detail_scalar(edge.get("to"))),
                        ),
                    )
                )
                continue
            from_activity_id, to_activity_id = outcome
            canonical_travel.append(migrated_edge)
            assignments.extend(
                (
                    IdAssignment(
                        entity_kind="travel_from_reference",
                        source_path=f"{edge_path}.from_activity_id",
                        stable_id=from_activity_id,
                        preserved="from_activity_id" in edge,
                    ),
                    IdAssignment(
                        entity_kind="travel_to_reference",
                        source_path=f"{edge_path}.to_activity_id",
                        stable_id=to_activity_id,
                        preserved="to_activity_id" in edge,
                    ),
                )
            )
        if "travel" in day or canonical_travel:
            day["travel"] = canonical_travel

    constraints_value = trip.get("constraints")
    if constraints_value is not None:
        constraints = _require_list(
            constraints_value,
            f"{source.trip_path}:$.constraints",
            nonempty=False,
        )
        for index, constraint_value in enumerate(constraints):
            constraint_path = f"{source.trip_path}:$.constraints[{index}]"
            constraint = _require_object(constraint_value, constraint_path)
            constraint_id, preserved = _existing_or_generated_id(
                constraint,
                primary="constraint_id",
                alias="id",
                generated=_stable_id("constraint", trip_id, str(index)),
                path=constraint_path,
            )
            _record_unique(
                constraint_id,
                seen_constraint_ids,
                "constraint",
                f"{constraint_path}.constraint_id",
            )
            constraint["constraint_id"] = constraint_id
            assignments.append(
                IdAssignment(
                    entity_kind="constraint",
                    source_path=f"{constraint_path}.constraint_id",
                    stable_id=constraint_id,
                    preserved=preserved,
                )
            )

    trip[MIGRATION_META_KEY] = {
        "migration": {
            "source_schema": "legacy-v1",
            "source_revision": source.source_revision,
            "protected_activity_ids": list(protected_activity_ids),
            "ignored_travel_edges": ignored_travel_edges,
        }
    }

    plan = build_plan(
        trip_id=trip_id,
        generation=generation,
        state={"trip": trip, "itinerary": itinerary},
        receipts={},
    )

    # This invokes the Phase 0 loader against isolated compatibility views.
    # It catches malformed dates, activity shape, constraints, and other domain
    # problems that are intentionally outside the persistence codec.
    try:
        plan_to_trip_state(plan)
    except LoadError as exc:
        raise MigrationError(
            "DOMAIN_VALIDATION_FAILED",
            str(exc),
            path=str(source.data_dir),
        ) from exc

    candidate_bytes = encode_plan(plan)
    candidate_revision = str(plan["revision"])
    assignments_tuple = tuple(assignments)
    issues_tuple = tuple(issues)
    preview_digest = _preview_digest(
        source.source_revision,
        candidate_bytes,
        assignments_tuple,
        issues_tuple,
    )
    preview = MigrationPreview(
        data_dir=source.data_dir,
        target_path=target_path,
        source_revision=source.source_revision,
        source_file_hashes=source.source_file_hashes,
        candidate_revision=candidate_revision,
        preview_digest=preview_digest,
        candidate_bytes=candidate_bytes,
        candidate_plan=freeze_json(plan),
        id_assignments=assignments_tuple,
        issues=issues_tuple,
        protected_activity_ids=tuple(protected_activity_ids),
    )
    preview.verify_candidate_bytes()
    return preview


def _find_data_dir(path: Path) -> Path:
    if path.is_dir() and (path / "trip.json").is_file():
        data_dir = path
    elif path.is_dir() and (path / "data" / "trip.json").is_file():
        data_dir = path / "data"
    else:
        raise MigrationError(
            "MISSING_LEGACY_DATA",
            "expected a directory containing data/trip.json and itinerary.json",
            path=str(path),
        )
    if not (data_dir / "itinerary.json").is_file():
        raise MigrationError(
            "MISSING_LEGACY_DATA",
            "itinerary.json is missing",
            path=str(data_dir / "itinerary.json"),
        )
    return data_dir


def _resolved_fallback_slug(data_dir: Path) -> str:
    """Derive path fallback identity from the physical source directory.

    Diagnostic paths retain the spelling supplied by the caller, but stable
    identity must not change when the same directory is reached through a
    relative path or symlink alias.
    """

    try:
        resolved = data_dir.resolve(strict=True)
    except OSError as exc:
        raise MigrationError(
            "READ_ERROR",
            str(exc),
            path=str(data_dir),
        ) from exc
    return resolved.parent.name if resolved.name == "data" else resolved.name


def _existing_or_generated_id(
    value: Mapping[str, Any],
    *,
    primary: str,
    alias: str | None,
    generated: str,
    path: str,
) -> tuple[str, bool]:
    primary_value = value.get(primary)
    alias_value = value.get(alias) if alias is not None else None
    if primary_value is not None and alias_value is not None:
        primary_id = _require_text(
            primary_value, f"{path}.{primary}", code="MALFORMED_LEGACY"
        )
        alias_id = _require_text(
            alias_value, f"{path}.{alias}", code="MALFORMED_LEGACY"
        )
        if primary_id != alias_id:
            raise MigrationError(
                "IDENTITY_MISMATCH",
                f"{primary} and {alias} contain different IDs",
                path=path,
            )
        return primary_id, True
    if primary_value is not None:
        return (
            _require_text(
                primary_value,
                f"{path}.{primary}",
                code="MALFORMED_LEGACY",
            ),
            True,
        )
    if alias_value is not None and alias is not None:
        return (
            _require_text(
                alias_value,
                f"{path}.{alias}",
                code="MALFORMED_LEGACY",
            ),
            True,
        )
    return generated, False


def _stable_id(kind: str, *parts: str) -> str:
    token = "\0".join((kind, *parts))
    return f"{kind}-{uuid.uuid5(_MIGRATION_NAMESPACE, token).hex}"


def _location_id(
    activity: Mapping[str, Any],
    *,
    trip_id: str,
    source_path: str,
) -> tuple[str, bool]:
    explicit = activity.get("location_id")
    if explicit is not None:
        return (
            _require_text(
                explicit,
                f"{source_path}.location_id",
                code="MALFORMED_LEGACY",
            ),
            True,
        )

    place_id = activity.get("place_id")
    if isinstance(place_id, str) and place_id.strip():
        identity = f"google-place:{place_id.strip()}"
        return identity, False

    lat = activity.get("lat")
    lng = activity.get("lng")
    if (
        not isinstance(lat, bool)
        and isinstance(lat, (int, float))
        and not isinstance(lng, bool)
        and isinstance(lng, (int, float))
        and math.isfinite(float(lat))
        and math.isfinite(float(lng))
    ):
        identity = f"coordinates:{float(lat):.6f},{float(lng):.6f}"
    else:
        query = activity.get("maps_query")
        title = activity.get("title", activity.get("display_name"))
        raw_identity = (
            query
            if isinstance(query, str) and query.strip()
            else _require_text(
                title,
                f"{source_path}.title",
                code="MALFORMED_LEGACY",
            )
        )
        identity = " ".join(raw_identity.casefold().split())
    return _stable_id("location", trip_id, identity), False


def _add_travel_endpoint_ids(
    edge: dict[str, Any],
    *,
    ordered_activity_ids: list[str],
    activity_index_by_id: Mapping[str, int],
    source_path: str,
) -> tuple[str, str] | None:
    from_id_value = edge.get("from_activity_id")
    to_id_value = edge.get("to_activity_id")
    from_index_value = edge.get("from")
    to_index_value = edge.get("to")

    if from_id_value is None:
        from_index = _legacy_index_or_none(
            from_index_value,
            len(ordered_activity_ids),
            f"{source_path}.from",
        )
        if from_index is None:
            return None
        from_activity_id = ordered_activity_ids[from_index]
    else:
        from_activity_id = _require_text(
            from_id_value,
            f"{source_path}.from_activity_id",
            code="MALFORMED_LEGACY",
        )
        if from_activity_id not in activity_index_by_id:
            raise MigrationError(
                "MALFORMED_REFERENCE",
                f"unknown from_activity_id {from_activity_id!r}",
                path=f"{source_path}.from_activity_id",
            )

    if to_id_value is None:
        to_index = _legacy_index_or_none(
            to_index_value,
            len(ordered_activity_ids),
            f"{source_path}.to",
        )
        if to_index is None:
            return None
        to_activity_id = ordered_activity_ids[to_index]
    else:
        to_activity_id = _require_text(
            to_id_value,
            f"{source_path}.to_activity_id",
            code="MALFORMED_LEGACY",
        )
        if to_activity_id not in activity_index_by_id:
            raise MigrationError(
                "MALFORMED_REFERENCE",
                f"unknown to_activity_id {to_activity_id!r}",
                path=f"{source_path}.to_activity_id",
            )

    if from_index_value is not None:
        _require_matching_index(
            from_index_value,
            activity_index_by_id[from_activity_id],
            f"{source_path}.from",
        )
    if to_index_value is not None:
        _require_matching_index(
            to_index_value,
            activity_index_by_id[to_activity_id],
            f"{source_path}.to",
        )

    edge["from_activity_id"] = from_activity_id
    edge["to_activity_id"] = to_activity_id
    return from_activity_id, to_activity_id


def _legacy_index_or_none(value: Any, length: int, path: str) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MigrationError(
            "MALFORMED_LEGACY",
            "travel endpoint must be an integer index",
            path=path,
        )
    if not 0 <= value < length:
        return None
    return value


def _require_matching_index(value: Any, expected: int, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MigrationError(
            "MALFORMED_LEGACY",
            "travel endpoint must be an integer index",
            path=path,
        )
    if value != expected:
        raise MigrationError(
            "TRAVEL_REFERENCE_MISMATCH",
            f"travel endpoint index {value} does not match stable ID index {expected}",
            path=path,
        )


def _record_unique(
    value: str, seen: set[str], kind: str, path: str
) -> None:
    if value in seen:
        raise MigrationError(
            "DUPLICATE_ID",
            f"duplicate {kind} ID {value!r}",
            path=path,
        )
    seen.add(value)


def _require_text(value: Any, path: str, *, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(
            code,
            "expected a non-empty string",
            path=path,
        )
    return value.strip()


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MigrationError(
            "MALFORMED_LEGACY",
            "expected an object",
            path=path,
        )
    return value


def _require_list(
    value: Any,
    path: str,
    *,
    nonempty: bool,
) -> list[Any]:
    if not isinstance(value, list):
        raise MigrationError(
            "MALFORMED_LEGACY",
            "expected an array",
            path=path,
        )
    if nonempty and not value:
        raise MigrationError(
            "MALFORMED_LEGACY",
            "array cannot be empty",
            path=path,
        )
    return value


def _detail_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _preview_digest(
    source_revision: str,
    candidate_bytes: bytes,
    assignments: Sequence[IdAssignment],
    issues: Sequence[MigrationIssue],
) -> str:
    metadata = {
        "source_revision": source_revision,
        "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "id_assignments": [
            {
                "entity_kind": assignment.entity_kind,
                "stable_id": assignment.stable_id,
                "preserved": assignment.preserved,
            }
            for assignment in assignments
        ],
        "issues": [
            {
                "code": issue.code,
                "severity": issue.severity,
                "message": issue.message,
                "details": {key: value for key, value in issue.details},
            }
            for issue in issues
        ],
    }
    return hashlib.sha256(
        _PREVIEW_DIGEST_DOMAIN + canonical_json_bytes(metadata)
    ).hexdigest()


# Friendly aliases for command and API layers.
preview_migration = preview_legacy_migration
migrate_preview = preview_legacy_migration


__all__ = [
    "MigrationError",
    "IdAssignment",
    "MigrationIssue",
    "LegacyDocuments",
    "MigrationPreview",
    "compute_legacy_source_revision",
    "read_legacy_documents",
    "legacy_source_revision",
    "preview_legacy_migration",
    "preview_migration",
    "migrate_preview",
]
