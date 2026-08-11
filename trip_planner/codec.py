"""Strict codec for the canonical Trip Planner document.

The persisted document is deliberately small and lossless::

    {
      "schema_version": "trip-planner.plan/v1",
      "trip_id": "...",
      "generation": 1,
      "revision": "...",
      "state": {
        "trip": { ...legacy-compatible trip fields... },
        "itinerary": { ...legacy-compatible itinerary fields... }
      },
      "receipts": {}
    }

``revision`` covers schema version, trip identity, generation, and state.
Receipts are intentionally excluded so an idempotency receipt can be recorded
without pretending that the user's plan changed.  ``generation`` is still
covered and must be advanced by mutation code, preventing an ABA state from
reusing an older revision.

This module performs no writes.  It also never returns internal mutable state:
builders and compatibility views recursively copy their inputs, while
``freeze_json`` is available for callers that need an immutable snapshot.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from .models import TripState


SCHEMA_VERSION = "trip-planner.plan/v1"
"""The only canonical schema version understood by this codec."""

MIGRATION_META_KEY = "_trip_planner"
"""Reserved revisioned metadata key inside ``state.trip``."""

_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRANSACTION_ID_RE = re.compile(r"^tx-[0-9a-f]{32}$")
_CANONICAL_LODGING_LOCATION_RE = re.compile(
    r"^lodging-location-[0-9a-f]{64}$"
)
_LODGING_KINDS = frozenset(
    {
        "unspecified",
        "hotel",
        "hostel",
        "ryokan",
        "guesthouse",
        "short_term_rental",
        "apartment",
        "homestay",
        "other",
    }
)
_RECEIPT_KINDS = {"create", "patch", "rollback"}
_RECEIPT_STATUSES = {"applied", "rolled_back"}
_RECEIPT_COMMON_FIELDS = {
    "kind",
    "status",
    "request_digest",
    "transaction_id",
    "applied_revision",
    "applied_generation",
}
_RECEIPT_CREATE_FIELDS = {
    "expected_absent",
    "candidate_sha256",
    "source_binding_digest",
}
_MAX_IDENTITY_LENGTH = 256
_MAX_RECEIPT_KEY_LENGTH = 256
_TOP_LEVEL_KEYS = {
    "schema_version",
    "trip_id",
    "generation",
    "revision",
    "state",
    "receipts",
}

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FrozenJsonValue: TypeAlias = (
    JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]
)


class PlanCodecError(ValueError):
    """A stable, machine-readable canonical document error."""

    def __init__(self, code: str, message: str, *, path: str = "$") -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code}: {message} ({path})")


class DuplicateKeyError(PlanCodecError):
    """Raised when JSON text contains the same object key more than once."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            "DUPLICATE_JSON_KEY",
            f"JSON object contains duplicate key {key!r}",
        )


def deep_copy_json(value: Any) -> JsonValue:
    """Return a recursive mutable copy of a JSON-compatible value.

    Unlike ``copy.deepcopy``, this rejects Python-only values and non-finite
    numbers at the trust boundary.  Tuples and read-only mappings produced by
    :func:`freeze_json` are accepted and thawed into JSON arrays/objects.
    """

    return _copy_json(value, "$")


def freeze_json(value: Any) -> FrozenJsonValue:
    """Return a deeply immutable snapshot of a JSON-compatible value."""

    copied = deep_copy_json(value)
    return _freeze_copied_json(copied)


def thaw_json(value: Any) -> JsonValue:
    """Return a detached mutable JSON value from a frozen or mutable input."""

    return deep_copy_json(value)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON-compatible value into deterministic UTF-8 bytes."""

    copied = deep_copy_json(value)
    try:
        text = json.dumps(
            copied,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:  # defensive; copy already validates
        raise PlanCodecError("INVALID_JSON_VALUE", str(exc)) from exc
    return text.encode("utf-8")


def decode_json_bytes(data: bytes | bytearray | memoryview | str) -> JsonValue:
    """Decode strict JSON, rejecting duplicate keys and NaN/Infinity."""

    if isinstance(data, str):
        text = data
    elif isinstance(data, (bytes, bytearray, memoryview)):
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlanCodecError("INVALID_JSON", str(exc)) from exc
    else:
        raise TypeError("JSON input must be bytes-like or str")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except PlanCodecError:
        raise
    except json.JSONDecodeError as exc:
        raise PlanCodecError(
            "INVALID_JSON",
            exc.msg,
            path=f"$ (line {exc.lineno}, column {exc.colno})",
        ) from exc
    return deep_copy_json(value)


def build_plan(
    *,
    trip_id: str,
    generation: int,
    state: Mapping[str, Any],
    receipts: Mapping[str, Any] | None = None,
) -> dict[str, JsonValue]:
    """Build and validate a detached canonical plan document."""

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "trip_id": trip_id,
        "generation": generation,
        "revision": "",
        "state": deep_copy_json(state),
        "receipts": deep_copy_json(receipts or {}),
    }
    plan["revision"] = compute_revision(plan)
    validate_plan(plan)
    return deep_copy_json(plan)  # type: ignore[return-value]


def compute_revision(plan: Mapping[str, Any]) -> str:
    """Compute the canonical state revision, excluding receipts and itself."""

    _require_mapping(plan, "$")
    missing = {
        key
        for key in ("schema_version", "trip_id", "generation", "state")
        if key not in plan
    }
    if missing:
        raise PlanCodecError(
            "MALFORMED_PLAN",
            f"missing revision fields: {', '.join(sorted(missing))}",
        )
    payload = {
        "schema_version": plan["schema_version"],
        "trip_id": plan["trip_id"],
        "generation": plan["generation"],
        "state": plan["state"],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def validate_plan(plan: Mapping[str, Any]) -> None:
    """Validate canonical structure, identity, references, and revision.

    Validation is read-only.  Location IDs are required but may repeat because
    multiple activities can intentionally refer to the same real-world place.
    Day, activity, and constraint IDs must be unique.
    """

    _require_mapping(plan, "$")
    deep_copy_json(plan)  # validates the complete JSON value, including receipts

    actual_keys = set(plan)
    missing = _TOP_LEVEL_KEYS - actual_keys
    extra = actual_keys - _TOP_LEVEL_KEYS
    if missing:
        raise PlanCodecError(
            "MALFORMED_PLAN",
            f"missing top-level fields: {', '.join(sorted(missing))}",
        )
    if extra:
        raise PlanCodecError(
            "MALFORMED_PLAN",
            f"unknown top-level fields: {', '.join(sorted(extra))}",
        )

    schema_version = plan["schema_version"]
    if schema_version != SCHEMA_VERSION:
        if not isinstance(schema_version, str):
            raise PlanCodecError(
                "MALFORMED_PLAN",
                "schema_version must be a string",
                path="$.schema_version",
            )
        raise PlanCodecError(
            "UNSUPPORTED_SCHEMA",
            f"unsupported schema version {schema_version!r}",
            path="$.schema_version",
        )

    trip_id = _require_text(plan["trip_id"], "$.trip_id")
    generation = plan["generation"]
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise PlanCodecError(
            "MALFORMED_PLAN",
            "generation must be an integer greater than or equal to 1",
            path="$.generation",
        )

    revision = plan["revision"]
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise PlanCodecError(
            "MALFORMED_PLAN",
            "revision must be a lowercase SHA-256 hex digest",
            path="$.revision",
        )

    state = _require_mapping(plan["state"], "$.state")
    state_keys = set(state)
    if state_keys != {"trip", "itinerary"}:
        missing_state = {"trip", "itinerary"} - state_keys
        extra_state = state_keys - {"trip", "itinerary"}
        details: list[str] = []
        if missing_state:
            details.append(f"missing {', '.join(sorted(missing_state))}")
        if extra_state:
            details.append(f"unknown {', '.join(sorted(extra_state))}")
        raise PlanCodecError(
            "MALFORMED_PLAN",
            f"state must contain only trip and itinerary ({'; '.join(details)})",
            path="$.state",
        )
    trip = _require_mapping(state["trip"], "$.state.trip")
    itinerary = _require_mapping(state["itinerary"], "$.state.itinerary")
    _validate_receipts(plan["receipts"])

    nested_trip_id = trip.get("trip_id")
    if nested_trip_id is not None:
        nested_trip_id = _require_text(
            nested_trip_id, "$.state.trip.trip_id"
        )
        if nested_trip_id != trip_id:
            raise PlanCodecError(
                "IDENTITY_MISMATCH",
                "state.trip.trip_id does not match top-level trip_id",
                path="$.state.trip.trip_id",
            )

    days_value = itinerary.get("days")
    days = _require_list(days_value, "$.state.itinerary.days")
    if not days:
        raise PlanCodecError(
            "MALFORMED_PLAN",
            "itinerary.days cannot be empty",
            path="$.state.itinerary.days",
        )

    day_ids: set[str] = set()
    activity_ids: set[str] = set()
    location_ids: set[str] = set()

    for day_index, day_value in enumerate(days):
        day_path = f"$.state.itinerary.days[{day_index}]"
        day = _require_mapping(day_value, day_path)
        day_id = _required_stable_id(day, "day_id", day_path)
        _reject_duplicate_id(day_id, day_ids, "day", f"{day_path}.day_id")
        if "id" in day and day["id"] != day_id:
            raise PlanCodecError(
                "IDENTITY_MISMATCH",
                "legacy day id alias does not match day_id",
                path=f"{day_path}.id",
            )
        for role in ("start", "end"):
            lodging_id = day.get(f"{role}_lodging_id")
            location_id = day.get(f"{role}_location_id")
            if lodging_id is not None:
                if location_id is None:
                    raise PlanCodecError(
                        "MALFORMED_LODGING",
                        "day lodging reference requires a location",
                        path=day_path,
                    )
                _require_text(lodging_id, f"{day_path}.{role}_lodging_id")
                _require_text(location_id, f"{day_path}.{role}_location_id")

        places = _require_list(day.get("places"), f"{day_path}.places")
        ordered_activity_ids: list[str] = []
        for activity_index, activity_value in enumerate(places):
            activity_path = f"{day_path}.places[{activity_index}]"
            activity = _require_mapping(activity_value, activity_path)
            activity_id = _required_stable_id(
                activity, "activity_id", activity_path
            )
            _reject_duplicate_id(
                activity_id,
                activity_ids,
                "activity",
                f"{activity_path}.activity_id",
            )
            if "id" in activity and activity["id"] != activity_id:
                raise PlanCodecError(
                    "IDENTITY_MISMATCH",
                    "legacy activity id alias does not match activity_id",
                    path=f"{activity_path}.id",
                )
            location_ids.add(
                _required_stable_id(activity, "location_id", activity_path)
            )
            ordered_activity_ids.append(activity_id)
        travel_value = day.get("travel", [])
        travel = _require_list(travel_value, f"{day_path}.travel")
        activity_index_by_id = {
            activity_id: index
            for index, activity_id in enumerate(ordered_activity_ids)
        }
        for edge_index, edge_value in enumerate(travel):
            edge_path = f"{day_path}.travel[{edge_index}]"
            edge = _require_mapping(edge_value, edge_path)
            from_activity_id = _required_stable_id(
                edge, "from_activity_id", edge_path
            )
            to_activity_id = _required_stable_id(
                edge, "to_activity_id", edge_path
            )
            if from_activity_id not in activity_index_by_id:
                raise PlanCodecError(
                    "MALFORMED_REFERENCE",
                    f"unknown from_activity_id {from_activity_id!r}",
                    path=f"{edge_path}.from_activity_id",
                )
            if to_activity_id not in activity_index_by_id:
                raise PlanCodecError(
                    "MALFORMED_REFERENCE",
                    f"unknown to_activity_id {to_activity_id!r}",
                    path=f"{edge_path}.to_activity_id",
                )
            _validate_legacy_edge_index(
                edge,
                "from",
                activity_index_by_id[from_activity_id],
                edge_path,
            )
            _validate_legacy_edge_index(
                edge,
                "to",
                activity_index_by_id[to_activity_id],
                edge_path,
            )
            modes = edge.get("modes")
            if modes is not None:
                _require_mapping(modes, f"{edge_path}.modes")
            recommended = edge.get("recommended_mode")
            if recommended is not None:
                recommended_text = _require_text(
                    recommended, f"{edge_path}.recommended_mode"
                )
                if isinstance(modes, Mapping) and recommended_text not in modes:
                    raise PlanCodecError(
                        "MALFORMED_REFERENCE",
                        f"recommended mode {recommended_text!r} has no estimate",
                        path=f"{edge_path}.recommended_mode",
                    )

    lodgings_value = trip.get("lodgings", [])
    lodgings = _require_list(lodgings_value, "$.state.trip.lodgings")
    lodging_by_id: dict[str, Mapping[str, Any]] = {}
    parsed_lodgings: list[
        tuple[date, date, str, Mapping[str, Any]]
    ] = []
    for index, value in enumerate(lodgings):
        path = f"$.state.trip.lodgings[{index}]"
        item = _require_mapping(value, path)
        expected_fields = {
            "lodging_id",
            "location_id",
            "check_in",
            "check_out",
            "kind",
            "decision_state",
            "evidence_state",
        }
        if set(item) != expected_fields:
            raise PlanCodecError(
                "MALFORMED_LODGING",
                "lodging has invalid fields",
                path=path,
            )
        lodging_id = _require_text(item["lodging_id"], f"{path}.lodging_id")
        if lodging_id in lodging_by_id:
            raise PlanCodecError(
                "DUPLICATE_ID",
                "duplicate lodging ID",
                path=f"{path}.lodging_id",
            )
        location_id = _require_text(
            item["location_id"],
            f"{path}.location_id",
        )
        if _CANONICAL_LODGING_LOCATION_RE.fullmatch(location_id) is None:
            raise PlanCodecError(
                "MALFORMED_LODGING",
                "lodging location ID must be privacy-safe and canonical",
                path=f"{path}.location_id",
            )
        try:
            check_in = date.fromisoformat(item["check_in"])
            check_out = date.fromisoformat(item["check_out"])
        except (TypeError, ValueError) as exc:
            raise PlanCodecError(
                "MALFORMED_LODGING",
                "lodging dates must be ISO dates",
                path=path,
            ) from exc
        if (
            check_in.isoformat() != item["check_in"]
            or check_out.isoformat() != item["check_out"]
            or check_out <= check_in
        ):
            raise PlanCodecError(
                "MALFORMED_LODGING",
                "lodging stay dates are invalid",
                path=path,
            )
        if (
            item["kind"] not in _LODGING_KINDS
            or item["decision_state"] not in {"selected", "fixed", "booked"}
            or item["evidence_state"] != "unverified"
        ):
            raise PlanCodecError(
                "MALFORMED_LODGING",
                "lodging enum values are invalid",
                path=path,
            )
        lodging_by_id[lodging_id] = item
        parsed_lodgings.append(
            (check_in, check_out, lodging_id, item)
        )

    parsed_lodgings.sort(key=lambda value: (value[0], value[1], value[2]))
    for previous, current in zip(
        parsed_lodgings,
        parsed_lodgings[1:],
    ):
        if previous[1] != current[0]:
            raise PlanCodecError(
                "MALFORMED_LODGING",
                "lodging stays must be contiguous without gaps or overlaps",
                path="$.state.trip.lodgings",
            )

    day_by_date: dict[str, Mapping[str, Any]] = {}
    for day_index, day_value in enumerate(days):
        day_path = f"$.state.itinerary.days[{day_index}]"
        day = _require_mapping(day_value, day_path)
        day_date = day.get("date")
        if parsed_lodgings:
            if type(day_date) is not str:
                raise PlanCodecError(
                    "MALFORMED_LODGING",
                    "lodging confirmation requires exact ISO itinerary dates",
                    path=f"{day_path}.date",
                )
            try:
                parsed_day_date = date.fromisoformat(day_date)
            except ValueError as exc:
                raise PlanCodecError(
                    "MALFORMED_LODGING",
                    "lodging confirmation requires exact ISO itinerary dates",
                    path=f"{day_path}.date",
                ) from exc
            if parsed_day_date.isoformat() != day_date:
                raise PlanCodecError(
                    "MALFORMED_LODGING",
                    "lodging confirmation requires exact ISO itinerary dates",
                    path=f"{day_path}.date",
                )
            if day_date in day_by_date:
                raise PlanCodecError(
                    "MALFORMED_LODGING",
                    "lodging confirmation requires unique itinerary dates",
                    path=day_path,
                )
            day_by_date[day_date] = day
        elif isinstance(day_date, str):
            day_by_date[day_date] = day
        for role in ("start", "end"):
            lodging_id = day.get(f"{role}_lodging_id")
            if lodging_id is None:
                continue
            item = lodging_by_id.get(lodging_id)
            if (
                item is None
                or item["location_id"]
                != day.get(f"{role}_location_id")
            ):
                raise PlanCodecError(
                    "MALFORMED_REFERENCE",
                    "day lodging reference does not match lodging location",
                    path=day_path,
                )

    expected_end: dict[str, str] = {}
    expected_start: dict[str, str] = {}
    for check_in, check_out, lodging_id, _ in parsed_lodgings:
        cursor = check_in
        while cursor < check_out:
            day_date = cursor.isoformat()
            if day_date not in day_by_date:
                raise PlanCodecError(
                    "MALFORMED_LODGING",
                    "every lodging night must have an itinerary day",
                    path="$.state.trip.lodgings",
                )
            expected_end[day_date] = lodging_id
            next_date = date.fromordinal(cursor.toordinal() + 1)
            next_text = next_date.isoformat()
            if next_text in day_by_date:
                expected_start[next_text] = lodging_id
            cursor = next_date

    first_date = (
        parsed_lodgings[0][0].isoformat()
        if parsed_lodgings
        else None
    )
    for day_date, day in day_by_date.items():
        if day.get("end_lodging_id") != expected_end.get(day_date):
            raise PlanCodecError(
                "MALFORMED_LODGING",
                "day end lodging does not exactly cover its night",
                path="$.state.itinerary.days",
            )
        actual_start = day.get("start_lodging_id")
        expected = expected_start.get(day_date)
        if (
            first_date is not None
            and day_date == first_date
            and actual_start == expected_end.get(day_date)
        ):
            expected = actual_start
        if actual_start != expected:
            raise PlanCodecError(
                "MALFORMED_LODGING",
                "day start lodging does not exactly cover the preceding night",
                path="$.state.itinerary.days",
            )

    constraints_value = trip.get("constraints", [])
    if constraints_value is None:
        constraints_value = []
    constraints = _require_list(
        constraints_value, "$.state.trip.constraints"
    )
    constraint_ids: set[str] = set()
    known_subject_ids = day_ids | activity_ids
    for index, constraint_value in enumerate(constraints):
        constraint_path = f"$.state.trip.constraints[{index}]"
        constraint = _require_mapping(constraint_value, constraint_path)
        constraint_id = _required_stable_id(
            constraint, "constraint_id", constraint_path
        )
        _reject_duplicate_id(
            constraint_id,
            constraint_ids,
            "constraint",
            f"{constraint_path}.constraint_id",
        )
        if "id" in constraint and constraint["id"] != constraint_id:
            raise PlanCodecError(
                "IDENTITY_MISMATCH",
                "legacy constraint id alias does not match constraint_id",
                path=f"{constraint_path}.id",
            )
        subjects_value = constraint.get("subject_ids", [])
        subjects = _require_list(
            subjects_value, f"{constraint_path}.subject_ids"
        )
        for subject_index, subject_value in enumerate(subjects):
            subject_path = f"{constraint_path}.subject_ids[{subject_index}]"
            subject = _require_text(subject_value, subject_path)
            if subject not in known_subject_ids:
                raise PlanCodecError(
                    "MALFORMED_REFERENCE",
                    f"constraint refers to unknown subject {subject!r}",
                    path=subject_path,
                )

    _reject_cross_kind_id_collisions(
        {
            "trip": {trip_id},
            "day": day_ids,
            "activity": activity_ids,
            "location": location_ids,
            "constraint": constraint_ids,
        }
    )
    _validate_migration_metadata(trip, activity_ids, day_ids)

    expected_revision = compute_revision(plan)
    if revision != expected_revision:
        raise PlanCodecError(
            "REVISION_MISMATCH",
            f"stored revision {revision!r} does not match {expected_revision!r}",
            path="$.revision",
        )


def encode_plan(plan: Mapping[str, Any]) -> bytes:
    """Validate and return deterministic canonical plan bytes."""

    validate_plan(plan)
    return canonical_json_bytes(plan)


def decode_plan(data: bytes | bytearray | memoryview | str) -> dict[str, JsonValue]:
    """Strictly decode and validate canonical plan bytes."""

    value = decode_json_bytes(data)
    if not isinstance(value, dict):
        raise PlanCodecError(
            "MALFORMED_PLAN",
            "canonical plan root must be an object",
        )
    validate_plan(value)
    return deep_copy_json(value)  # type: ignore[return-value]


def load_plan(path: str | Path) -> dict[str, JsonValue]:
    """Read and strictly decode one canonical plan file."""

    plan_path = Path(path)
    try:
        data = plan_path.read_bytes()
    except OSError as exc:
        raise PlanCodecError(
            "READ_ERROR",
            str(exc),
            path=str(plan_path),
        ) from exc
    return decode_plan(data)


def legacy_compatibility_views(
    plan: Mapping[str, Any],
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """Return detached ``trip.json`` and ``itinerary.json`` compatible views.

    Positional travel indices are always derived from stable activity endpoint
    IDs.  Migration-only metadata is removed.  Invalid derived legacy travel
    fragments retained by a migration preview are restored only in this view;
    they never become canonical edges consumed by the planning kernel.
    """

    validate_plan(plan)
    state = _require_mapping(plan["state"], "$.state")
    trip = deep_copy_json(state["trip"])
    itinerary = deep_copy_json(state["itinerary"])
    assert isinstance(trip, dict)
    assert isinstance(itinerary, dict)

    migration_meta = trip.pop(MIGRATION_META_KEY, None)
    days = itinerary["days"]
    assert isinstance(days, list)
    day_by_id: dict[str, dict[str, Any]] = {}
    for day in days:
        assert isinstance(day, dict)
        day_id = day["day_id"]
        assert isinstance(day_id, str)
        day_by_id[day_id] = day
        places = day["places"]
        assert isinstance(places, list)
        index_by_id = {
            place["activity_id"]: index
            for index, place in enumerate(places)
            if isinstance(place, dict)
        }
        travel = day.get("travel", [])
        assert isinstance(travel, list)
        for edge in travel:
            assert isinstance(edge, dict)
            edge["from"] = index_by_id[edge["from_activity_id"]]
            edge["to"] = index_by_id[edge["to_activity_id"]]

    for fragment in _ignored_travel_fragments(migration_meta):
        day_id = fragment["day_id"]
        day = day_by_id.get(day_id)
        if day is None:
            continue
        travel = day.setdefault("travel", [])
        if not isinstance(travel, list):
            continue
        index = fragment["index"]
        edge = deep_copy_json(fragment["edge"])
        travel.insert(min(index, len(travel)), edge)

    return trip, itinerary


def plan_to_trip_state(plan: Mapping[str, Any]) -> "TripState":
    """Project a canonical plan into the existing immutable kernel aggregate.

    The canonical document is converted to detached compatibility values and
    parsed entirely in memory.  The resulting aggregate is relabeled with the
    canonical schema and revision rather than the compatibility-view hash.
    """

    validate_plan(plan)
    trip, itinerary = legacy_compatibility_views(plan)
    if (
        not isinstance(trip.get("slug"), str)
        or not str(trip["slug"]).strip()
    ):
        trip["slug"] = str(plan["trip_id"])
    _omit_blank_legacy_optional_fields(trip, itinerary)

    from .loaders import _load_legacy_values

    state = _load_legacy_values(
        trip,
        itinerary,
        trip_bytes=canonical_json_bytes(trip),
        itinerary_bytes=canonical_json_bytes(itinerary),
        fallback_slug=str(plan["trip_id"]),
    )

    return replace(
        state,
        schema_version=str(plan["schema_version"]),
        revision=str(plan["revision"]),
    )


def _omit_blank_legacy_optional_fields(
    trip: dict[str, Any],
    itinerary: dict[str, Any],
) -> None:
    """Adapt harmless legacy empty strings to the Phase 0 loader contract.

    Older generators intentionally emitted ``""`` for optional presentation
    fields, while the Phase 0 loader treats a present optional string as
    non-empty.  Omitting those blank values in the private compatibility copy
    preserves the canonical document and lets the loader apply its documented
    default.
    """

    _pop_blank_fields(trip, ("subtitle", "timezone"))
    constraints = trip.get("constraints", [])
    if isinstance(constraints, list):
        for constraint in constraints:
            if isinstance(constraint, dict):
                _pop_blank_fields(constraint, ("origin", "source_text"))

    days = itinerary.get("days", [])
    if not isinstance(days, list):
        return
    for day in days:
        if not isinstance(day, dict):
            continue
        _pop_blank_fields(
            day,
            (
                "title",
                "subtitle",
                "timezone",
                "available_start",
                "available_end",
                "start_location_id",
                "end_location_id",
            ),
        )
        places = day.get("places", [])
        if isinstance(places, list):
            for place in places:
                if isinstance(place, dict):
                    _pop_blank_fields(
                        place,
                        ("note", "maps_query", "type", "time"),
                    )
        travel = day.get("travel", [])
        if not isinstance(travel, list):
            continue
        for edge in travel:
            if not isinstance(edge, dict):
                continue
            _pop_blank_fields(
                edge,
                (
                    "source",
                    "recommended_mode",
                    "fresh_until",
                    "evidence_state",
                ),
            )
            modes = edge.get("modes")
            if isinstance(modes, dict):
                for mode in modes.values():
                    if isinstance(mode, dict):
                        _pop_blank_fields(
                            mode,
                            ("fresh_until", "evidence_state"),
                        )


def _pop_blank_fields(value: dict[str, Any], fields: Sequence[str]) -> None:
    for field in fields:
        if value.get(field) == "":
            value.pop(field, None)


def _copy_json(value: Any, path: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PlanCodecError(
                "NON_FINITE_NUMBER",
                "JSON numbers must be finite",
                path=path,
            )
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PlanCodecError(
                    "INVALID_JSON_VALUE",
                    "JSON object keys must be strings",
                    path=path,
                )
            result[key] = _copy_json(item, _child_path(path, key))
        return result
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return [
            _copy_json(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise PlanCodecError(
        "INVALID_JSON_VALUE",
        f"unsupported JSON value type {type(value).__name__}",
        path=path,
    )


def _freeze_copied_json(value: JsonValue) -> FrozenJsonValue:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_copied_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_copied_json(item) for item in value)
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise PlanCodecError(
        "NON_FINITE_NUMBER",
        f"JSON constant {value!r} is not permitted",
    )


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanCodecError(
            "MALFORMED_PLAN",
            "expected an object",
            path=path,
        )
    return value


def _require_list(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        raise PlanCodecError(
            "MALFORMED_PLAN",
            "expected an array",
            path=path,
        )
    return value


def _require_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanCodecError(
            "MISSING_ID" if path.endswith("_id") else "MALFORMED_PLAN",
            "expected a non-empty string",
            path=path,
        )
    if (
        len(value) > _MAX_IDENTITY_LENGTH
        or value != value.strip()
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise PlanCodecError(
            "MALFORMED_PLAN",
            (
                "identity and reference strings must be 1-256 visible "
                "characters without leading/trailing whitespace or "
                "control characters"
            ),
            path=path,
        )
    return value


def _validate_receipts(value: Any) -> None:
    receipts = _require_mapping(value, "$.receipts")
    transaction_owner: dict[str, str] = {}
    for key, receipt_value in receipts.items():
        key_path = _child_path("$.receipts", key)
        if (
            not key
            or len(key) > _MAX_RECEIPT_KEY_LENGTH
            or key != key.strip()
            or any(
                unicodedata.category(character).startswith("C")
                for character in key
            )
        ):
            raise PlanCodecError(
                "MALFORMED_RECEIPT",
                (
                    "receipt keys must be 1-256 visible characters without "
                    "leading/trailing whitespace or control characters"
                ),
                path=key_path,
            )

        if not isinstance(receipt_value, Mapping):
            raise PlanCodecError(
                "MALFORMED_RECEIPT",
                "receipt must be an object",
                path=key_path,
            )
        receipt = receipt_value
        kind = receipt.get("kind")
        required = set(_RECEIPT_COMMON_FIELDS)
        if kind == "create":
            required.update(_RECEIPT_CREATE_FIELDS)
        else:
            required.add("base_revision")
        missing = required - set(receipt)
        if missing:
            raise PlanCodecError(
                "MALFORMED_RECEIPT",
                f"receipt is missing fields: {', '.join(sorted(missing))}",
                path=key_path,
            )

        _require_receipt_enum(
            kind,
            f"{key_path}.kind",
            _RECEIPT_KINDS,
        )
        _require_receipt_enum(
            receipt["status"],
            f"{key_path}.status",
            _RECEIPT_STATUSES,
        )
        request_digest = receipt["request_digest"]
        if (
            not isinstance(request_digest, str)
            or not _REQUEST_DIGEST_RE.fullmatch(request_digest)
        ):
            raise PlanCodecError(
                "MALFORMED_RECEIPT",
                (
                    "request_digest must be sha256: followed by "
                    "64 lowercase hex characters"
                ),
                path=f"{key_path}.request_digest",
            )
        transaction_id = receipt["transaction_id"]
        if (
            not isinstance(transaction_id, str)
            or not _TRANSACTION_ID_RE.fullmatch(transaction_id)
        ):
            raise PlanCodecError(
                "MALFORMED_RECEIPT",
                (
                    "transaction_id must be tx- followed by "
                    "32 lowercase hex characters"
                ),
                path=f"{key_path}.transaction_id",
            )
        previous_key = transaction_owner.get(transaction_id)
        if previous_key is not None:
            raise PlanCodecError(
                "MALFORMED_RECEIPT",
                (
                    f"transaction_id {transaction_id!r} is shared by "
                    f"receipt keys {previous_key!r} and {key!r}"
                ),
                path=f"{key_path}.transaction_id",
            )
        transaction_owner[transaction_id] = key

        for field in (
            *(("base_revision",) if kind != "create" else ()),
            "applied_revision",
        ):
            revision_value = receipt[field]
            if (
                not isinstance(revision_value, str)
                or not _REVISION_RE.fullmatch(revision_value)
            ):
                raise PlanCodecError(
                    "MALFORMED_RECEIPT",
                    f"{field} must be a lowercase SHA-256 hex digest",
                    path=f"{key_path}.{field}",
                )
        if kind == "create":
            if receipt["status"] != "applied":
                raise PlanCodecError(
                    "MALFORMED_RECEIPT",
                    "create receipts must remain applied",
                    path=f"{key_path}.status",
                )
            if receipt["expected_absent"] is not True:
                raise PlanCodecError(
                    "MALFORMED_RECEIPT",
                    "create receipts must bind an absent target",
                    path=f"{key_path}.expected_absent",
                )
            for field in ("candidate_sha256", "source_binding_digest"):
                digest_value = receipt[field]
                if (
                    not isinstance(digest_value, str)
                    or not _REVISION_RE.fullmatch(digest_value)
                ):
                    raise PlanCodecError(
                        "MALFORMED_RECEIPT",
                        f"{field} must be a lowercase SHA-256 hex digest",
                        path=f"{key_path}.{field}",
                    )
        applied_generation = receipt["applied_generation"]
        if (
            isinstance(applied_generation, bool)
            or not isinstance(applied_generation, int)
            or applied_generation < 1
        ):
            raise PlanCodecError(
                "MALFORMED_RECEIPT",
                (
                    "applied_generation must be an integer greater than "
                    "or equal to 1"
                ),
                path=f"{key_path}.applied_generation",
            )

        # Keep receipt objects extensible while validating optional fields that
        # participate in rollback identity or integrity when present.
        for field in ("target_transaction_id", "rolled_back_by"):
            optional_transaction_id = receipt.get(field)
            if optional_transaction_id is not None and (
                not isinstance(optional_transaction_id, str)
                or not _TRANSACTION_ID_RE.fullmatch(optional_transaction_id)
            ):
                raise PlanCodecError(
                    "MALFORMED_RECEIPT",
                    (
                        f"{field} must be tx- followed by "
                        "32 lowercase hex characters"
                    ),
                    path=f"{key_path}.{field}",
                )
        for field in (
            "rolled_back_revision",
            "before_snapshot_sha256",
        ):
            optional_digest = receipt.get(field)
            if optional_digest is not None and (
                not isinstance(optional_digest, str)
                or not _REVISION_RE.fullmatch(optional_digest)
            ):
                raise PlanCodecError(
                    "MALFORMED_RECEIPT",
                    f"{field} must be a lowercase SHA-256 hex digest",
                    path=f"{key_path}.{field}",
                )
        evaluation_at = receipt.get("evaluation_at")
        if evaluation_at is not None:
            if not isinstance(evaluation_at, str):
                raise PlanCodecError(
                    "MALFORMED_RECEIPT",
                    "evaluation_at must be a normalized UTC ISO timestamp",
                    path=f"{key_path}.evaluation_at",
                )
            try:
                parsed_evaluation_at = datetime.fromisoformat(evaluation_at)
            except ValueError as exc:
                raise PlanCodecError(
                    "MALFORMED_RECEIPT",
                    "evaluation_at must be a normalized UTC ISO timestamp",
                    path=f"{key_path}.evaluation_at",
                ) from exc
            if (
                parsed_evaluation_at.tzinfo is None
                or parsed_evaluation_at.utcoffset() is None
                or evaluation_at
                != parsed_evaluation_at.astimezone(timezone.utc).isoformat()
            ):
                raise PlanCodecError(
                    "MALFORMED_RECEIPT",
                    "evaluation_at must be a normalized UTC ISO timestamp",
                    path=f"{key_path}.evaluation_at",
                )


def _require_receipt_enum(
    value: Any,
    path: str,
    allowed: set[str],
) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise PlanCodecError(
            "MALFORMED_RECEIPT",
            f"expected one of: {', '.join(sorted(allowed))}",
            path=path,
        )


def _required_stable_id(
    value: Mapping[str, Any], field: str, object_path: str
) -> str:
    path = f"{object_path}.{field}"
    if field not in value:
        raise PlanCodecError(
            "MISSING_ID",
            f"missing stable ID field {field!r}",
            path=path,
        )
    return _require_text(value[field], path)


def _reject_duplicate_id(
    value: str, seen: set[str], kind: str, path: str
) -> None:
    if value in seen:
        raise PlanCodecError(
            "DUPLICATE_ID",
            f"duplicate {kind} ID {value!r}",
            path=path,
        )
    seen.add(value)


def _reject_cross_kind_id_collisions(
    ids_by_kind: Mapping[str, set[str]],
) -> None:
    owner_by_id: dict[str, str] = {}
    for kind, values in ids_by_kind.items():
        for stable_id in values:
            previous = owner_by_id.get(stable_id)
            if previous is not None and previous != kind:
                raise PlanCodecError(
                    "DUPLICATE_ID",
                    (
                        f"stable ID {stable_id!r} is shared by "
                        f"{previous} and {kind} entities"
                    ),
                    path="$",
                )
            owner_by_id[stable_id] = kind


def _validate_legacy_edge_index(
    edge: Mapping[str, Any],
    field: str,
    expected: int,
    edge_path: str,
) -> None:
    if field not in edge:
        return
    value = edge[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanCodecError(
            "MALFORMED_PLAN",
            f"travel {field} index must be an integer",
            path=f"{edge_path}.{field}",
        )
    if value != expected:
        endpoint_field = f"{field}_activity_id"
        raise PlanCodecError(
            "TRAVEL_REFERENCE_MISMATCH",
            (
                f"travel {field} index {value} does not identify "
                f"{endpoint_field} {edge[endpoint_field]!r}; expected {expected}"
            ),
            path=f"{edge_path}.{field}",
        )


def _validate_migration_metadata(
    trip: Mapping[str, Any],
    activity_ids: set[str],
    day_ids: set[str],
) -> None:
    metadata_value = trip.get(MIGRATION_META_KEY)
    if metadata_value is None:
        return
    metadata = _require_mapping(
        metadata_value, f"$.state.trip.{MIGRATION_META_KEY}"
    )
    migration_value = metadata.get("migration")
    if migration_value is None:
        return
    migration = _require_mapping(
        migration_value,
        f"$.state.trip.{MIGRATION_META_KEY}.migration",
    )
    protected_value = migration.get("protected_activity_ids", [])
    protected = _require_list(
        protected_value,
        (
            f"$.state.trip.{MIGRATION_META_KEY}.migration."
            "protected_activity_ids"
        ),
    )
    seen: set[str] = set()
    for index, activity_value in enumerate(protected):
        path = (
            f"$.state.trip.{MIGRATION_META_KEY}.migration."
            f"protected_activity_ids[{index}]"
        )
        activity_id = _require_text(activity_value, path)
        _reject_duplicate_id(activity_id, seen, "protected activity", path)
        if activity_id not in activity_ids:
            raise PlanCodecError(
                "MALFORMED_REFERENCE",
                f"protected activity {activity_id!r} does not exist",
                path=path,
            )

    fragments_value = migration.get("ignored_travel_edges", [])
    fragments = _require_list(
        fragments_value,
        (
            f"$.state.trip.{MIGRATION_META_KEY}.migration."
            "ignored_travel_edges"
        ),
    )
    for index, fragment_value in enumerate(fragments):
        path = (
            f"$.state.trip.{MIGRATION_META_KEY}.migration."
            f"ignored_travel_edges[{index}]"
        )
        fragment = _require_mapping(fragment_value, path)
        day_id = _require_text(fragment.get("day_id"), f"{path}.day_id")
        if day_id not in day_ids:
            raise PlanCodecError(
                "MALFORMED_REFERENCE",
                f"ignored travel fragment refers to unknown day {day_id!r}",
                path=f"{path}.day_id",
            )
        fragment_index = fragment.get("index")
        if (
            isinstance(fragment_index, bool)
            or not isinstance(fragment_index, int)
            or fragment_index < 0
        ):
            raise PlanCodecError(
                "MALFORMED_PLAN",
                "ignored travel edge index must be a non-negative integer",
                path=f"{path}.index",
            )
        _require_mapping(fragment.get("edge"), f"{path}.edge")


def _ignored_travel_fragments(metadata_value: Any) -> list[dict[str, Any]]:
    if not isinstance(metadata_value, Mapping):
        return []
    migration = metadata_value.get("migration")
    if not isinstance(migration, Mapping):
        return []
    fragments = migration.get("ignored_travel_edges")
    if not isinstance(fragments, Sequence) or isinstance(
        fragments, (str, bytes, bytearray, memoryview)
    ):
        return []
    return [
        fragment
        for fragment in fragments
        if isinstance(fragment, dict)
    ]


def _child_path(path: str, key: str) -> str:
    if key.isidentifier():
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


# Concise aliases for callers that prefer serialization terminology.
canonical_plan_bytes = encode_plan
dumps_plan = encode_plan
loads_plan = decode_plan
legacy_views = legacy_compatibility_views


__all__ = [
    "SCHEMA_VERSION",
    "MIGRATION_META_KEY",
    "PlanCodecError",
    "DuplicateKeyError",
    "JsonValue",
    "FrozenJsonValue",
    "deep_copy_json",
    "freeze_json",
    "thaw_json",
    "canonical_json_bytes",
    "decode_json_bytes",
    "build_plan",
    "compute_revision",
    "validate_plan",
    "encode_plan",
    "canonical_plan_bytes",
    "decode_plan",
    "load_plan",
    "dumps_plan",
    "loads_plan",
    "legacy_compatibility_views",
    "legacy_views",
    "plan_to_trip_state",
]
