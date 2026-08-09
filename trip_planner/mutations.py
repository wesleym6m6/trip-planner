"""Pure semantic mutation engine for canonical trip plans.

This module deliberately has no filesystem, locking, revision-generation, or
receipt-writing behavior.  It applies a :class:`PlanPatch` to an in-memory copy
of a canonical plan and returns a structured :class:`PatchDraft` for a store to
validate and commit.

Patch operations target persisted entity IDs only.  JSON array indices and
filesystem paths are never part of the public contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import InitVar, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence, TypeAlias


JsonMapping: TypeAlias = Mapping[str, Any]

PATCH_VERSION = "plan-patch/v1"
PLAN_PATCH_MAX_OPERATIONS = 128
_MIGRATION_META_KEY = "_trip_planner"
_MAX_REQUEST_ID_LENGTH = 256
_MAX_PATCH_VERSION_LENGTH = 64
_MAX_PATCH_INTENT_LENGTH = 4096
_MAX_SERIALIZED_PATCH_BYTES = 1024 * 1024
_LODGING_GRANT_TOKEN = object()
_CANONICAL_LODGING_LOCATION_RE = re.compile(
    r"lodging-location-[0-9a-f]{64}"
)
_SHA256_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
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
_LODGING_ACTIVITY_TYPES = frozenset(
    {
        "accommodation",
        "airbnb",
        "apartment",
        "guesthouse",
        "homestay",
        "hostel",
        "hotel",
        "inn",
        "lodging",
        "motel",
        "resort",
        "ryokan",
        "short_term_rental",
        "stay",
        "vacation_rental",
    }
)

ACTIVITY_MUTABLE_FIELDS = frozenset(
    {
        "allowed_windows",
        "decision_state",
        "display_name",
        "duration_min",
        "evidence_state",
        "flexibility",
        "lat",
        "lng",
        "location_id",
        "maps_query",
        "note",
        "place_id",
        "priority",
        "time",
        "title",
        "type",
    }
)
ACTIVITY_IDENTITY_FIELDS = frozenset(
    {"activity_id", "id", "day_id", "order"}
)
DAY_MUTABLE_FIELDS = frozenset(
    {
        "allowed_modes",
        "available_end",
        "available_start",
        "date",
        "day",
        "subtitle",
        "timezone",
        "title",
    }
)
DAY_IDENTITY_FIELDS = frozenset({"day_id", "id", "places", "travel"})
CONSTRAINT_MUTABLE_FIELDS = frozenset(
    {
        "confidence",
        "kind",
        "origin",
        "params",
        "source_text",
        "strength",
        "subject_ids",
    }
)
CONSTRAINT_IDENTITY_FIELDS = frozenset({"constraint_id", "id"})

_ACTIVITY_TRAVEL_FIELDS = frozenset(
    {
        "allowed_windows",
        "decision_state",
        "duration_min",
        "lat",
        "lng",
        "location_id",
        "maps_query",
        "place_id",
        "time",
    }
)
_ACTIVITY_EVIDENCE_FIELDS = frozenset(
    {
        "allowed_windows",
        "display_name",
        "duration_min",
        "lat",
        "lng",
        "location_id",
        "maps_query",
        "place_id",
    }
)
_DAY_TRAVEL_FIELDS = frozenset(
    {
        "allowed_modes",
        "available_end",
        "available_start",
        "date",
        "day",
        "end_location_id",
        "start_location_id",
        "timezone",
    }
)
_PROTECTED_DECISIONS = frozenset({"fixed", "booked"})
_PROTECTED_FLEXIBILITY = frozenset({"fixed_day", "fixed_time"})
_DECISION_RANK = {
    "cancelled": -2,
    "excluded": -2,
    "candidate": 0,
    "selected": 1,
    "fixed": 2,
    "booked": 3,
}
_FLEXIBILITY_RANK = {"movable": 0, "fixed_day": 1, "fixed_time": 2}


class Placement(str, Enum):
    """Stable-ID-relative placement for an activity."""

    START = "start"
    END = "end"
    BEFORE = "before"
    AFTER = "after"


class MigratedActivityClassificationKind(str, Enum):
    """Small human-owned vocabulary for one migrated activity."""

    MOVABLE = "movable"
    FIXED_DAY = "fixed_day"
    FIXED_TIME = "fixed_time"
    BOOKED = "booked"

    @property
    def decision_state(self) -> str:
        return {
            type(self).MOVABLE: "selected",
            type(self).FIXED_DAY: "fixed",
            type(self).FIXED_TIME: "fixed",
            type(self).BOOKED: "booked",
        }[self]

    @property
    def flexibility(self) -> str:
        return {
            type(self).MOVABLE: "movable",
            type(self).FIXED_DAY: "fixed_day",
            type(self).FIXED_TIME: "fixed_time",
            type(self).BOOKED: "fixed_time",
        }[self]


@dataclass(frozen=True, slots=True)
class _UnsetValue:
    """Sentinel that distinguishes an omitted time from an explicit clear."""


UNSET = _UnsetValue()


@dataclass(frozen=True, slots=True)
class AddActivity:
    op_id: str
    activity_id: str
    day_id: str
    fields: JsonMapping
    position: Placement = Placement.END
    anchor_activity_id: str | None = None

    def __post_init__(self) -> None:
        _require_request_id(self.op_id, "AddActivity.op_id")
        _require_request_id(self.activity_id, "AddActivity.activity_id")
        _require_request_id(self.day_id, "AddActivity.day_id")
        object.__setattr__(self, "fields", _freeze_mapping(self.fields, "fields"))
        object.__setattr__(self, "position", _coerce_placement(self.position))
        _optional_request_id(
            self.anchor_activity_id, "AddActivity.anchor_activity_id"
        )


@dataclass(frozen=True, slots=True)
class ConfirmedLodgingStay:
    """One privacy-safe canonical stay chosen by a human."""

    lodging_id: str
    location_id: str
    check_in: str
    check_out: str
    kind: str
    decision_state: str
    evidence_state: str = "unverified"

    def __post_init__(self) -> None:
        _require_request_id(
            self.lodging_id,
            "ConfirmedLodgingStay.lodging_id",
        )
        _require_canonical_lodging_location_id(
            self.location_id,
            "ConfirmedLodgingStay.location_id",
        )
        for value, name in (
            (self.check_in, "check_in"),
            (self.check_out, "check_out"),
        ):
            _lodging_date(value, f"ConfirmedLodgingStay.{name}")
        if self.check_out <= self.check_in:
            raise ValueError(
                "ConfirmedLodgingStay.check_out must follow check_in"
            )
        if self.kind not in _LODGING_KINDS:
            raise ValueError("Unsupported confirmed lodging kind")
        if self.decision_state not in {"selected", "fixed", "booked"}:
            raise ValueError(
                "Confirmed lodging decision must be selected, fixed, or booked"
            )
        if self.evidence_state != "unverified":
            raise ValueError("Confirmed lodging evidence_state must be unverified")


@dataclass(frozen=True, slots=True)
class LodgingAnchorAssignment:
    day_id: str
    start_lodging_id: str | None = None
    end_lodging_id: str | None = None

    def __post_init__(self) -> None:
        _require_request_id(self.day_id, "LodgingAnchorAssignment.day_id")
        _optional_request_id(
            self.start_lodging_id,
            "LodgingAnchorAssignment.start_lodging_id",
        )
        _optional_request_id(
            self.end_lodging_id,
            "LodgingAnchorAssignment.end_lodging_id",
        )
        if self.start_lodging_id is None and self.end_lodging_id is None:
            raise ValueError(
                "LodgingAnchorAssignment requires a start or end lodging"
            )


@dataclass(frozen=True, slots=True)
class SetLodgingSelection:
    """Replace canonical lodging and its day anchors as one typed operation."""

    op_id: str
    stays: tuple[ConfirmedLodgingStay, ...]
    anchors: tuple[LodgingAnchorAssignment, ...]
    selection_binding_digest: str

    def __post_init__(self) -> None:
        _require_request_id(self.op_id, "SetLodgingSelection.op_id")
        object.__setattr__(self, "stays", tuple(self.stays))
        object.__setattr__(self, "anchors", tuple(self.anchors))
        if not self.stays or any(
            type(item) is not ConfirmedLodgingStay for item in self.stays
        ):
            raise TypeError(
                "SetLodgingSelection.stays must contain confirmed stays"
            )
        if any(
            type(item) is not LodgingAnchorAssignment
            for item in self.anchors
        ):
            raise TypeError(
                "SetLodgingSelection.anchors must contain lodging anchors"
            )
        _require_sha256_digest(
            self.selection_binding_digest,
            "SetLodgingSelection.selection_binding_digest",
            prefixed=False,
        )


@dataclass(frozen=True, slots=True)
class MigratedActivityClassification:
    """One explicit human classification for a protected legacy activity."""

    activity_id: str
    kind: MigratedActivityClassificationKind

    def __post_init__(self) -> None:
        _require_request_id(
            self.activity_id,
            "MigratedActivityClassification.activity_id",
        )
        if type(self.kind) is not MigratedActivityClassificationKind:
            raise TypeError(
                "MigratedActivityClassification.kind must be exact"
            )


@dataclass(frozen=True, slots=True)
class AdoptMigratedBaseline:
    """Classify every protected legacy activity and adopt it atomically."""

    op_id: str
    source_revision: str
    classifications: tuple[MigratedActivityClassification, ...]

    def __post_init__(self) -> None:
        _require_request_id(self.op_id, "AdoptMigratedBaseline.op_id")
        _require_request_id(
            self.source_revision,
            "AdoptMigratedBaseline.source_revision",
        )
        values = tuple(self.classifications)
        if not values or any(
            type(item) is not MigratedActivityClassification
            for item in values
        ):
            raise TypeError(
                "AdoptMigratedBaseline.classifications must be non-empty and exact"
            )
        ordered = tuple(sorted(values, key=lambda item: item.activity_id))
        if len({item.activity_id for item in ordered}) != len(ordered):
            raise ValueError(
                "AdoptMigratedBaseline classifications must have unique activity IDs"
            )
        object.__setattr__(self, "classifications", ordered)


@dataclass(frozen=True, slots=True)
class UpdateActivity:
    op_id: str
    activity_id: str
    fields: JsonMapping

    def __post_init__(self) -> None:
        _require_request_id(self.op_id, "UpdateActivity.op_id")
        _require_request_id(self.activity_id, "UpdateActivity.activity_id")
        object.__setattr__(self, "fields", _freeze_mapping(self.fields, "fields"))


@dataclass(frozen=True, slots=True)
class PlaceActivity:
    op_id: str
    activity_id: str
    day_id: str
    position: Placement = Placement.END
    anchor_activity_id: str | None = None
    scheduled_start: str | None | _UnsetValue = UNSET

    def __post_init__(self) -> None:
        _require_request_id(self.op_id, "PlaceActivity.op_id")
        _require_request_id(self.activity_id, "PlaceActivity.activity_id")
        _require_request_id(self.day_id, "PlaceActivity.day_id")
        object.__setattr__(self, "position", _coerce_placement(self.position))
        _optional_request_id(
            self.anchor_activity_id, "PlaceActivity.anchor_activity_id"
        )
        if (
            self.scheduled_start is not UNSET
            and self.scheduled_start is not None
            and not isinstance(self.scheduled_start, str)
        ):
            raise TypeError("PlaceActivity.scheduled_start must be text, None, or UNSET")


@dataclass(frozen=True, slots=True)
class RemoveActivity:
    op_id: str
    activity_id: str

    def __post_init__(self) -> None:
        _require_request_id(self.op_id, "RemoveActivity.op_id")
        _require_request_id(self.activity_id, "RemoveActivity.activity_id")


@dataclass(frozen=True, slots=True)
class UpdateDay:
    op_id: str
    day_id: str
    fields: JsonMapping

    def __post_init__(self) -> None:
        _require_request_id(self.op_id, "UpdateDay.op_id")
        _require_request_id(self.day_id, "UpdateDay.day_id")
        object.__setattr__(self, "fields", _freeze_mapping(self.fields, "fields"))


@dataclass(frozen=True, slots=True)
class AddConstraint:
    op_id: str
    constraint_id: str
    fields: JsonMapping

    def __post_init__(self) -> None:
        _require_request_id(self.op_id, "AddConstraint.op_id")
        _require_request_id(self.constraint_id, "AddConstraint.constraint_id")
        object.__setattr__(self, "fields", _freeze_mapping(self.fields, "fields"))


@dataclass(frozen=True, slots=True)
class UpdateConstraint:
    op_id: str
    constraint_id: str
    fields: JsonMapping

    def __post_init__(self) -> None:
        _require_request_id(self.op_id, "UpdateConstraint.op_id")
        _require_request_id(
            self.constraint_id, "UpdateConstraint.constraint_id"
        )
        object.__setattr__(self, "fields", _freeze_mapping(self.fields, "fields"))


@dataclass(frozen=True, slots=True)
class RemoveConstraint:
    op_id: str
    constraint_id: str

    def __post_init__(self) -> None:
        _require_request_id(self.op_id, "RemoveConstraint.op_id")
        _require_request_id(
            self.constraint_id, "RemoveConstraint.constraint_id"
        )


PatchOperation: TypeAlias = (
    AdoptMigratedBaseline
    | AddActivity
    | UpdateActivity
    | PlaceActivity
    | RemoveActivity
    | UpdateDay
    | AddConstraint
    | UpdateConstraint
    | RemoveConstraint
    | SetLodgingSelection
)


@dataclass(frozen=True, slots=True)
class PlanPatch:
    """One idempotent semantic mutation proposal.

    Human approval is intentionally absent.  A trusted caller supplies
    :class:`ApprovalGrant` objects separately when asking a store to apply the
    returned draft.
    """

    trip_id: str
    base_revision: str
    idempotency_key: str
    operations: tuple[PatchOperation, ...]
    intent: str = ""
    patch_version: str = PATCH_VERSION

    def __post_init__(self) -> None:
        _require_request_id(self.trip_id, "PlanPatch.trip_id")
        _require_request_id(self.base_revision, "PlanPatch.base_revision")
        _require_request_id(
            self.idempotency_key, "PlanPatch.idempotency_key"
        )
        if not isinstance(self.intent, str):
            raise TypeError("PlanPatch.intent must be a string")
        if len(self.intent) > _MAX_PATCH_INTENT_LENGTH:
            raise ValueError(
                "PlanPatch.intent must be at most "
                f"{_MAX_PATCH_INTENT_LENGTH} characters"
            )
        _require_request_id(
            self.patch_version,
            "PlanPatch.patch_version",
            max_length=_MAX_PATCH_VERSION_LENGTH,
        )
        if not isinstance(self.operations, tuple):
            object.__setattr__(self, "operations", tuple(self.operations))
        if len(self.operations) > PLAN_PATCH_MAX_OPERATIONS:
            raise ValueError(
                "PlanPatch.operations must contain at most "
                f"{PLAN_PATCH_MAX_OPERATIONS} operations"
            )


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """Approval issued outside the AI-authored patch boundary."""

    approval_id: str
    scope_digest: str
    approved_by: str
    approved_at: str

    def __post_init__(self) -> None:
        _require_text(self.approval_id, "ApprovalGrant.approval_id")
        _require_text(self.scope_digest, "ApprovalGrant.scope_digest")
        _require_text(self.approved_by, "ApprovalGrant.approved_by")
        _require_text(self.approved_at, "ApprovalGrant.approved_at")


@dataclass(frozen=True, slots=True)
class LodgingConfirmationGrant:
    """Externally signed authority for one exact lodging mutation.

    Construction alone is never authorization.  A persistence host must also
    configure :class:`TripStore` with a verifier whose signing key or
    host-side grant registry is unavailable to the AI process.
    """

    review_id: str
    trip_id: str
    base_revision: str
    request_digest: str
    scope_digest: str
    confirmed_by: str
    confirmed_at: datetime
    expires_at: datetime
    issuer_id: str
    signature: str = field(repr=False)
    grant_id: str = field(init=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _LODGING_GRANT_TOKEN:
            raise ValueError("Lodging confirmation grants must be host-minted")
        _require_sha256_digest(
            self.review_id,
            "LodgingConfirmationGrant.review_id",
            prefixed=False,
        )
        _require_request_id(
            self.trip_id,
            "LodgingConfirmationGrant.trip_id",
        )
        _require_sha256_digest(
            self.base_revision,
            "LodgingConfirmationGrant.base_revision",
            prefixed=False,
        )
        _require_sha256_digest(
            self.request_digest,
            "LodgingConfirmationGrant.request_digest",
            prefixed=True,
        )
        _require_sha256_digest(
            self.scope_digest,
            "LodgingConfirmationGrant.scope_digest",
            prefixed=True,
        )
        _require_request_id(
            self.confirmed_by,
            "LodgingConfirmationGrant.confirmed_by",
        )
        _require_request_id(
            self.issuer_id,
            "LodgingConfirmationGrant.issuer_id",
        )
        _require_request_id(
            self.signature,
            "LodgingConfirmationGrant.signature",
            max_length=1024,
        )
        confirmed_at = _aware_utc_datetime(
            self.confirmed_at,
            "LodgingConfirmationGrant.confirmed_at",
        )
        expires_at = _aware_utc_datetime(
            self.expires_at,
            "LodgingConfirmationGrant.expires_at",
        )
        if expires_at <= confirmed_at:
            raise ValueError(
                "LodgingConfirmationGrant.expires_at must follow confirmed_at"
            )
        object.__setattr__(self, "confirmed_at", confirmed_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(
            self,
            "grant_id",
            _digest(
                {
                    "payload_sha256": hashlib.sha256(
                        self.verification_payload()
                    ).hexdigest(),
                    "signature": self.signature,
                }
            ),
        )

    def verification_payload(self) -> bytes:
        """Return the exact public bytes an external host must verify."""

        return lodging_confirmation_grant_payload(
            review_id=self.review_id,
            trip_id=self.trip_id,
            base_revision=self.base_revision,
            request_digest=self.request_digest,
            scope_digest=self.scope_digest,
            confirmed_by=self.confirmed_by,
            confirmed_at=self.confirmed_at,
            expires_at=self.expires_at,
            issuer_id=self.issuer_id,
        )


@dataclass(frozen=True, slots=True)
class MutationProblem:
    """Machine-readable reason a draft cannot be committed."""

    code: str
    message: str
    op_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    details: JsonMapping = MappingProxyType({})

    def __post_init__(self) -> None:
        _require_text(self.code, "MutationProblem.code")
        _require_text(self.message, "MutationProblem.message")
        _optional_text(self.op_id, "MutationProblem.op_id")
        _optional_text(self.entity_type, "MutationProblem.entity_type")
        _optional_text(self.entity_id, "MutationProblem.entity_id")
        object.__setattr__(
            self, "details", _freeze_mapping(self.details, "MutationProblem.details")
        )


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    """One explicit or derived change produced by a patch."""

    op_id: str
    entity_type: str
    entity_id: str
    field: str
    before: Any
    after: Any
    kind: str = "update"

    def __post_init__(self) -> None:
        _require_text(self.op_id, "ChangeRecord.op_id")
        _require_text(self.entity_type, "ChangeRecord.entity_type")
        _require_text(self.entity_id, "ChangeRecord.entity_id")
        _require_text(self.field, "ChangeRecord.field")
        _require_text(self.kind, "ChangeRecord.kind")
        object.__setattr__(self, "before", _freeze_json(self.before))
        object.__setattr__(self, "after", _freeze_json(self.after))

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "field": self.field,
            "before": _thaw_json(self.before),
            "after": _thaw_json(self.after),
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class PatchDraft:
    """Candidate plan and policy information returned by the pure engine."""

    plan: JsonMapping
    patch_digest: str
    changes: tuple[ChangeRecord, ...] = ()
    protected_changes: tuple[ChangeRecord, ...] = ()
    problems: tuple[MutationProblem, ...] = ()
    affected_day_ids: tuple[str, ...] = ()
    invalidated_day_ids: tuple[str, ...] = ()
    required_approval_scope: str | None = None
    approval_granted: bool = False
    required_lodging_confirmation_scope: str | None = None
    lodging_confirmation_granted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan", _freeze_mapping(self.plan, "PatchDraft.plan"))
        for value, name, expected in (
            (self.changes, "changes", ChangeRecord),
            (self.protected_changes, "protected_changes", ChangeRecord),
            (self.problems, "problems", MutationProblem),
        ):
            if not isinstance(value, tuple):
                raise TypeError(f"PatchDraft.{name} must be a tuple")
            if any(not isinstance(item, expected) for item in value):
                raise TypeError(f"PatchDraft.{name} contains an invalid item")
        if not isinstance(self.affected_day_ids, tuple):
            raise TypeError("PatchDraft.affected_day_ids must be a tuple")
        if not isinstance(self.invalidated_day_ids, tuple):
            raise TypeError("PatchDraft.invalidated_day_ids must be a tuple")
        _optional_text(
            self.required_approval_scope, "PatchDraft.required_approval_scope"
        )
        if not isinstance(self.approval_granted, bool):
            raise TypeError("PatchDraft.approval_granted must be bool")
        _optional_text(
            self.required_lodging_confirmation_scope,
            "PatchDraft.required_lodging_confirmation_scope",
        )
        if not isinstance(self.lodging_confirmation_granted, bool):
            raise TypeError("PatchDraft.lodging_confirmation_granted must be bool")

    @property
    def can_apply(self) -> bool:
        return not self.problems

    @property
    def candidate_plan(self) -> JsonMapping:
        """Compatibility alias emphasizing that the result is not committed."""

        return self.plan

    def to_plan_dict(self) -> dict[str, Any]:
        """Return an independent mutable JSON representation."""

        return _thaw_json(self.plan)


@dataclass(slots=True)
class _EntityIndex:
    days: dict[str, dict[str, Any]]
    activities: dict[str, tuple[dict[str, Any], int, dict[str, Any]]]
    constraints: dict[str, tuple[int, dict[str, Any]]]


def patch_to_dict(patch: PlanPatch) -> dict[str, Any]:
    """Return the deterministic JSON contract for ``patch``.

    Approval data is not serializable through this function by design.
    """

    operations: list[dict[str, Any]] = []
    for operation in patch.operations:
        if isinstance(operation, AddActivity):
            value = {
                "op": "add_activity",
                "op_id": operation.op_id,
                "activity_id": operation.activity_id,
                "day_id": operation.day_id,
                "fields": _thaw_json(operation.fields),
                "position": operation.position.value,
            }
            _add_anchor(value, operation.anchor_activity_id)
        elif isinstance(operation, UpdateActivity):
            value = {
                "op": "update_activity",
                "op_id": operation.op_id,
                "activity_id": operation.activity_id,
                "fields": _thaw_json(operation.fields),
            }
        elif isinstance(operation, PlaceActivity):
            value = {
                "op": "place_activity",
                "op_id": operation.op_id,
                "activity_id": operation.activity_id,
                "day_id": operation.day_id,
                "position": operation.position.value,
            }
            _add_anchor(value, operation.anchor_activity_id)
            if operation.scheduled_start is not UNSET:
                value["scheduled_start"] = operation.scheduled_start
        elif isinstance(operation, RemoveActivity):
            value = {
                "op": "remove_activity",
                "op_id": operation.op_id,
                "activity_id": operation.activity_id,
            }
        elif isinstance(operation, UpdateDay):
            value = {
                "op": "update_day",
                "op_id": operation.op_id,
                "day_id": operation.day_id,
                "fields": _thaw_json(operation.fields),
            }
        elif isinstance(operation, SetLodgingSelection):
            value = {
                "op": "set_lodging_selection",
                "op_id": operation.op_id,
                "selection_binding_digest": (
                    operation.selection_binding_digest
                ),
                "stays": [
                    {
                        "lodging_id": stay.lodging_id,
                        "location_id": stay.location_id,
                        "check_in": stay.check_in,
                        "check_out": stay.check_out,
                        "kind": stay.kind,
                        "decision_state": stay.decision_state,
                        "evidence_state": stay.evidence_state,
                    }
                    for stay in operation.stays
                ],
                "anchors": [
                    {
                        "day_id": anchor.day_id,
                        "start_lodging_id": anchor.start_lodging_id,
                        "end_lodging_id": anchor.end_lodging_id,
                    }
                    for anchor in operation.anchors
                ],
            }
        elif isinstance(operation, AdoptMigratedBaseline):
            value = {
                "op": "adopt_migrated_baseline",
                "op_id": operation.op_id,
                "source_revision": operation.source_revision,
                "classifications": [
                    {
                        "activity_id": item.activity_id,
                        "kind": item.kind.value,
                    }
                    for item in operation.classifications
                ],
            }
        elif isinstance(operation, AddConstraint):
            value = {
                "op": "add_constraint",
                "op_id": operation.op_id,
                "constraint_id": operation.constraint_id,
                "fields": _thaw_json(operation.fields),
            }
        elif isinstance(operation, UpdateConstraint):
            value = {
                "op": "update_constraint",
                "op_id": operation.op_id,
                "constraint_id": operation.constraint_id,
                "fields": _thaw_json(operation.fields),
            }
        elif isinstance(operation, RemoveConstraint):
            value = {
                "op": "remove_constraint",
                "op_id": operation.op_id,
                "constraint_id": operation.constraint_id,
            }
        else:
            raise TypeError(
                f"Unsupported patch operation {type(operation).__name__!r}"
            )
        operations.append(value)
    return {
        "patch_version": patch.patch_version,
        "trip_id": patch.trip_id,
        "base_revision": patch.base_revision,
        "idempotency_key": patch.idempotency_key,
        "intent": patch.intent,
        "operations": operations,
    }


def patch_digest(patch: PlanPatch) -> str:
    """Hash the complete AI-authored request using canonical JSON."""

    encoded = _canonical_json(patch_to_dict(patch)).encode("utf-8")
    if len(encoded) > _MAX_SERIALIZED_PATCH_BYTES:
        raise ValueError(
            "Serialized PlanPatch must be at most "
            f"{_MAX_SERIALIZED_PATCH_BYTES} bytes"
        )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def approval_scope_digest(
    *,
    trip_id: str,
    base_revision: str,
    request_digest: str,
    protected_changes: Sequence[ChangeRecord],
) -> str:
    """Bind human approval to one base state and exact protected net diff."""

    canonical_changes = sorted(
        (change.to_dict() for change in protected_changes),
        key=lambda value: (
            value["entity_type"],
            value["entity_id"],
            value["field"],
            _canonical_json(value["before"]),
            _canonical_json(value["after"]),
        ),
    )
    return _digest(
        {
            "trip_id": trip_id,
            "base_revision": base_revision,
            "patch_digest": request_digest,
            "protected_changes": canonical_changes,
        }
    )


def lodging_confirmation_scope_digest(
    *,
    trip_id: str,
    base_revision: str,
    request_digest: str,
    lodging_changes: Sequence[ChangeRecord],
) -> str:
    """Exact human-confirmation binding for canonical lodging changes."""
    return _digest(
        {
            "trip_id": trip_id,
            "base_revision": base_revision,
            "patch_digest": request_digest,
            "lodging_changes": sorted(
                (item.to_dict() for item in lodging_changes),
                key=_canonical_json,
            ),
        }
    )


def lodging_confirmation_grant_payload(
    *,
    review_id: str,
    trip_id: str,
    base_revision: str,
    request_digest: str,
    scope_digest: str,
    confirmed_by: str,
    confirmed_at: datetime,
    expires_at: datetime,
    issuer_id: str,
) -> bytes:
    """Return deterministic public bytes for an external host signature."""

    return _canonical_json(
        {
            "contract": "trip-planner.lodging-confirmation-grant/v1",
            "review_id": review_id,
            "trip_id": trip_id,
            "base_revision": base_revision,
            "request_digest": request_digest,
            "scope_digest": scope_digest,
            "confirmed_by": confirmed_by,
            "confirmed_at": _aware_utc_datetime(
                confirmed_at,
                "confirmed_at",
            ).isoformat(),
            "expires_at": _aware_utc_datetime(
                expires_at,
                "expires_at",
            ).isoformat(),
            "issuer_id": issuer_id,
        }
    ).encode("utf-8")


def _mint_lodging_confirmation_grant(
    *,
    review_id: str,
    trip_id: str,
    base_revision: str,
    request_digest: str,
    scope_digest: str,
    confirmed_by: str,
    confirmed_at: datetime,
    expires_at: datetime,
    issuer_id: str,
    signature: str,
) -> LodgingConfirmationGrant:
    """Build a signed grant value; persistence still verifies the signature."""

    return LodgingConfirmationGrant(
        review_id=review_id,
        trip_id=trip_id,
        base_revision=base_revision,
        request_digest=request_digest,
        scope_digest=scope_digest,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        expires_at=expires_at,
        issuer_id=issuer_id,
        signature=signature,
        _token=_LODGING_GRANT_TOKEN,
    )


def build_signed_lodging_confirmation_grant(
    *,
    review_id: str,
    trip_id: str,
    base_revision: str,
    request_digest: str,
    scope_digest: str,
    confirmed_by: str,
    confirmed_at: datetime,
    expires_at: datetime,
    issuer_id: str,
    signature: str,
) -> LodgingConfirmationGrant:
    """Build a signed envelope for later host verification.

    This builder does not confer authority.  ``TripStore`` accepts the value
    only when its separately configured host verifier validates the issuer,
    signature, lifetime, and any host-side revocation or one-time policy.
    """

    return _mint_lodging_confirmation_grant(
        review_id=review_id,
        trip_id=trip_id,
        base_revision=base_revision,
        request_digest=request_digest,
        scope_digest=scope_digest,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        expires_at=expires_at,
        issuer_id=issuer_id,
        signature=signature,
    )


def apply_patch_to_plan(
    plan: JsonMapping,
    patch: PlanPatch,
    *,
    approvals: Sequence[ApprovalGrant] = (),
    lodging_confirmations: Sequence[LodgingConfirmationGrant] = (),
) -> PatchDraft:
    """Apply ``patch`` to a deep JSON copy and return a non-persistent draft."""

    problems: list[MutationProblem] = []
    changes: list[ChangeRecord] = []
    affected_days: set[str] = set()
    invalidation_sources: dict[str, set[str]] = {}

    try:
        request_digest = patch_digest(patch)
    except (TypeError, ValueError) as exc:
        request_digest = ""
        problems.append(
            MutationProblem(
                code="PATCH_SCHEMA_INVALID",
                message=str(exc),
            )
        )

    try:
        original = _thaw_json(plan)
    except (TypeError, ValueError) as exc:
        original = {}
        problems.append(
            MutationProblem(code="MALFORMED_PLAN", message=str(exc))
        )
    candidate = _thaw_json(original)

    shape_problem = _validate_plan_shape(candidate)
    if shape_problem is not None:
        problems.append(shape_problem)

    if patch.patch_version != PATCH_VERSION:
        problems.append(
            MutationProblem(
                code="UNSUPPORTED_PATCH_VERSION",
                message=(
                    f"Unsupported patch version {patch.patch_version!r}; "
                    f"expected {PATCH_VERSION!r}."
                ),
            )
        )
    plan_trip_id = candidate.get("trip_id") if isinstance(candidate, dict) else None
    if plan_trip_id != patch.trip_id:
        problems.append(
            MutationProblem(
                code="TRIP_ID_MISMATCH",
                message=(
                    f"Patch targets trip {patch.trip_id!r}, but the plan is "
                    f"{plan_trip_id!r}."
                ),
                details={"expected": plan_trip_id, "actual": patch.trip_id},
            )
        )
    plan_revision = candidate.get("revision") if isinstance(candidate, dict) else None
    if plan_revision != patch.base_revision:
        problems.append(
            MutationProblem(
                code="STALE_REVISION",
                message=(
                    f"Patch base revision {patch.base_revision!r} does not match "
                    f"the observed revision {plan_revision!r}."
                ),
                details={
                    "base_revision": patch.base_revision,
                    "observed_revision": plan_revision,
                },
            )
        )

    duplicate_op_ids = _duplicates(
        operation.op_id
        for operation in patch.operations
        if hasattr(operation, "op_id") and isinstance(operation.op_id, str)
    )
    for op_id in duplicate_op_ids:
        problems.append(
            MutationProblem(
                code="DUPLICATE_OPERATION_ID",
                message=f"Patch contains duplicate operation ID {op_id!r}.",
                op_id=op_id,
            )
        )
    if not patch.operations:
        problems.append(
            MutationProblem(
                code="EMPTY_PATCH",
                message="A PlanPatch must contain at least one semantic operation.",
            )
        )
    adoption_count = sum(
        isinstance(operation, AdoptMigratedBaseline)
        for operation in patch.operations
    )
    if adoption_count and (
        adoption_count != 1 or len(patch.operations) != 1
    ):
        problems.append(
            MutationProblem(
                code="BASELINE_ADOPTION_MUST_BE_EXCLUSIVE",
                message=(
                    "Migrated baseline adoption must be the patch's only "
                    "semantic operation."
                ),
            )
        )

    blocking_prefix_codes = {
        "MALFORMED_PLAN",
        "UNSUPPORTED_PATCH_VERSION",
        "TRIP_ID_MISMATCH",
        "STALE_REVISION",
        "DUPLICATE_OPERATION_ID",
        "PATCH_SCHEMA_INVALID",
        "EMPTY_PATCH",
        "BASELINE_ADOPTION_MUST_BE_EXCLUSIVE",
    }
    if not any(problem.code in blocking_prefix_codes for problem in problems):
        initial_index, initial_problems = _build_index(candidate)
        problems.extend(initial_problems)
        if initial_index is not None and not initial_problems:
            for operation in patch.operations:
                if isinstance(operation, AdoptMigratedBaseline):
                    _apply_adopt_migrated_baseline(
                        candidate,
                        operation,
                        problems,
                        changes,
                        affected_days,
                    )
                elif isinstance(operation, AddActivity):
                    _apply_add_activity(
                        candidate,
                        operation,
                        problems,
                        changes,
                        affected_days,
                        invalidation_sources,
                    )
                elif isinstance(operation, UpdateActivity):
                    _apply_update_activity(
                        candidate,
                        operation,
                        problems,
                        changes,
                        affected_days,
                        invalidation_sources,
                    )
                elif isinstance(operation, PlaceActivity):
                    _apply_place_activity(
                        candidate,
                        operation,
                        problems,
                        changes,
                        affected_days,
                        invalidation_sources,
                    )
                elif isinstance(operation, RemoveActivity):
                    _apply_remove_activity(
                        candidate,
                        operation,
                        problems,
                        changes,
                        affected_days,
                        invalidation_sources,
                    )
                elif isinstance(operation, UpdateDay):
                    _apply_update_day(
                        candidate,
                        operation,
                        problems,
                        changes,
                        affected_days,
                        invalidation_sources,
                    )
                elif isinstance(operation, SetLodgingSelection):
                    _apply_set_lodging_selection(
                        candidate,
                        operation,
                        problems,
                        changes,
                        affected_days,
                        invalidation_sources,
                    )
                elif isinstance(operation, AddConstraint):
                    _apply_add_constraint(candidate, operation, problems, changes)
                elif isinstance(operation, UpdateConstraint):
                    _apply_update_constraint(candidate, operation, problems, changes)
                elif isinstance(operation, RemoveConstraint):
                    _apply_remove_constraint(candidate, operation, problems, changes)
                else:
                    problems.append(
                        MutationProblem(
                            code="UNKNOWN_OPERATION",
                            message=(
                                f"Unsupported operation "
                                f"{type(operation).__name__!r}."
                            ),
                            op_id=getattr(operation, "op_id", None),
                        )
                    )

            _, final_problems = _build_index(candidate)
            problems.extend(final_problems)
            _validate_constraint_references(candidate, problems)
            if not problems:
                _downgrade_changed_activity_evidence(
                    original,
                    candidate,
                    changes,
                    problems,
                )
                if not problems:
                    if isinstance(
                        patch.operations[0],
                        AdoptMigratedBaseline,
                    ):
                        invalidation_sources = {}
                    else:
                        invalidation_sources = _net_travel_invalidation_sources(
                            original,
                            candidate,
                        )
                    _invalidate_day_travel(
                        candidate,
                        invalidation_sources,
                        problems,
                        changes,
                    )
                    _synchronize_migration_metadata(
                        candidate,
                        invalidated_day_ids=frozenset(
                            invalidation_sources
                        ),
                        problems=problems,
                        changes=changes,
                    )

    if problems:
        return PatchDraft(
            plan=original,
            patch_digest=request_digest,
            problems=_deduplicate_problems(problems),
        )

    protected_changes = _protected_net_changes(
        original,
        candidate,
    )
    required_scope: str | None = None
    approval_granted = False
    if protected_changes and request_digest:
        required_scope = approval_scope_digest(
            trip_id=patch.trip_id,
            base_revision=patch.base_revision,
            request_digest=request_digest,
            protected_changes=protected_changes,
        )
        approval_granted = any(
            isinstance(grant, ApprovalGrant)
            and grant.scope_digest == required_scope
            for grant in approvals
        )
        if not approval_granted:
            code = "APPROVAL_SCOPE_MISMATCH" if approvals else "APPROVAL_REQUIRED"
            message = (
                "Provided approval does not match the exact protected change scope."
                if approvals
                else "Protected activity or hard-constraint changes require "
                "human approval."
            )
            problems.append(
                MutationProblem(
                    code=code,
                    message=message,
                    details={
                        "required_scope_digest": required_scope,
                        "protected_change_count": len(protected_changes),
                    },
                )
            )

    lodging_changes = tuple(
        change
        for change in changes
        if change.kind == "lodging_anchor"
        or change.entity_type == "lodging"
    )
    lodging_scope: str | None = None
    lodging_granted = False
    if lodging_changes and request_digest:
        lodging_scope = lodging_confirmation_scope_digest(
            trip_id=patch.trip_id,
            base_revision=patch.base_revision,
            request_digest=request_digest,
            lodging_changes=lodging_changes,
        )
        lodging_granted = any(
            type(grant) is LodgingConfirmationGrant
            and grant.trip_id == patch.trip_id
            and grant.base_revision == patch.base_revision
            and grant.request_digest == request_digest
            and grant.scope_digest == lodging_scope
            for grant in lodging_confirmations
        )
        if not lodging_granted:
            problems.append(
                MutationProblem(
                    code=(
                        "LODGING_CONFIRMATION_MISMATCH"
                        if lodging_confirmations
                        else "LODGING_CONFIRMATION_REQUIRED"
                    ),
                    message=(
                        "Canonical lodging changes require an exact "
                        "lodging confirmation grant."
                    ),
                    details={"required_scope_digest": lodging_scope},
                )
            )
    return PatchDraft(
        plan=candidate,
        patch_digest=request_digest,
        changes=tuple(changes),
        protected_changes=tuple(protected_changes),
        problems=_deduplicate_problems(problems),
        affected_day_ids=tuple(sorted(affected_days)),
        invalidated_day_ids=tuple(sorted(invalidation_sources)),
        required_approval_scope=required_scope,
        approval_granted=approval_granted,
        required_lodging_confirmation_scope=lodging_scope,
        lodging_confirmation_granted=lodging_granted,
    )


# Friendly aliases for callers that name the pure operation as a preview.
preview_patch = apply_patch_to_plan
apply_plan_patch = apply_patch_to_plan


def _apply_adopt_migrated_baseline(
    plan: dict[str, Any],
    operation: AdoptMigratedBaseline,
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
    affected_days: set[str],
) -> None:
    index = _operation_index(plan, operation.op_id, problems)
    if index is None:
        return
    trip = plan.get("state", {}).get("trip")
    metadata = (
        trip.get(_MIGRATION_META_KEY)
        if isinstance(trip, dict)
        else None
    )
    migration = (
        metadata.get("migration")
        if isinstance(metadata, dict)
        else None
    )
    protected = (
        migration.get("protected_activity_ids")
        if isinstance(migration, dict)
        else None
    )
    if not isinstance(protected, list) or any(
        not isinstance(activity_id, str) or not activity_id
        for activity_id in protected
    ):
        problems.append(
            _problem(
                "MIGRATED_BASELINE_UNAVAILABLE",
                "The canonical plan has no valid migrated baseline protection.",
                operation,
                "trip",
                str(plan.get("trip_id", "")) or "trip",
            )
        )
        return
    if not protected:
        problems.append(
            _problem(
                "MIGRATED_BASELINE_ALREADY_ADOPTED",
                "The migrated baseline no longer has protected activities.",
                operation,
                "trip",
                str(plan.get("trip_id", "")) or "trip",
            )
        )
        return
    if migration.get("source_schema") != "legacy-v1":
        problems.append(
            _problem(
                "MIGRATED_BASELINE_SOURCE_SCHEMA_UNSUPPORTED",
                "Only the legacy-v1 migration baseline can be adopted.",
                operation,
                "trip",
                str(plan.get("trip_id", "")) or "trip",
            )
        )
        return
    if migration.get("source_revision") != operation.source_revision:
        problems.append(
            _problem(
                "MIGRATED_BASELINE_SOURCE_CHANGED",
                "The migrated baseline source revision no longer matches.",
                operation,
                "trip",
                str(plan.get("trip_id", "")) or "trip",
            )
        )
        return

    classified_ids = {
        item.activity_id for item in operation.classifications
    }
    protected_ids = set(protected)
    if classified_ids != protected_ids:
        problems.append(
            _problem(
                "MIGRATED_BASELINE_CLASSIFICATION_MISMATCH",
                (
                    "Classifications must cover every protected migrated "
                    "activity exactly once."
                ),
                operation,
                "trip",
                str(plan.get("trip_id", "")) or "trip",
                details={
                    "protected_activity_count": len(protected_ids),
                    "classified_activity_count": len(classified_ids),
                    "missing_activity_count": len(
                        protected_ids.difference(classified_ids)
                    ),
                    "unexpected_activity_count": len(
                        classified_ids.difference(protected_ids)
                    ),
                },
            )
        )
        return

    classified_activities: list[
        tuple[
            MigratedActivityClassification,
            dict[str, Any],
            dict[str, Any],
        ]
    ] = []
    for classification in operation.classifications:
        found = index.activities.get(classification.activity_id)
        if found is None:  # codec metadata normally makes this unreachable
            problems.append(
                _problem(
                    "UNKNOWN_ACTIVITY",
                    "A classified migrated activity no longer exists.",
                    operation,
                    "activity",
                    classification.activity_id,
                )
            )
            continue
        day, _position, activity = found
        if activity.get("decision_state") in {
            "candidate",
            "cancelled",
            "excluded",
        }:
            problems.append(
                _problem(
                    "MIGRATED_BASELINE_REACTIVATION_FORBIDDEN",
                    (
                        "Baseline adoption cannot reactivate an inactive "
                        "migrated activity."
                    ),
                    operation,
                    "activity",
                    classification.activity_id,
                )
            )
            continue
        decision_state = classification.kind.decision_state
        flexibility = classification.kind.flexibility
        if flexibility == "fixed_time" and not activity.get("time"):
            problems.append(
                _problem(
                    "FIXED_TIME_REQUIRES_SCHEDULED_TIME",
                    (
                        "Fixed-time or booked migrated activities require an "
                        "existing scheduled time."
                    ),
                    operation,
                    "activity",
                    classification.activity_id,
                )
            )
            continue
        classified_activities.append((classification, day, activity))
    if problems:
        return

    for classification, day, activity in classified_activities:
        decision_state = classification.kind.decision_state
        flexibility = classification.kind.flexibility
        affected_days.add(str(day["day_id"]))
        for field, after in (
            ("decision_state", decision_state),
            ("flexibility", flexibility),
        ):
            before = _field_marker(activity, field)
            activity[field] = after
            if before != after:
                changes.append(
                    ChangeRecord(
                        op_id=operation.op_id,
                        entity_type="activity",
                        entity_id=classification.activity_id,
                        field=field,
                        before=before,
                        after=after,
                        kind="baseline_classification",
                    )
                )
    assert isinstance(migration, dict)
    migration["protected_activity_ids"] = []
    changes.append(
        ChangeRecord(
            op_id=operation.op_id,
            entity_type="trip",
            entity_id=str(plan.get("trip_id", "")) or "trip",
            field=(
                f"{_MIGRATION_META_KEY}.migration."
                "protected_activity_ids"
            ),
            before=protected,
            after=[],
            kind="baseline_adoption",
        )
    )


def _apply_add_activity(
    plan: dict[str, Any],
    operation: AddActivity,
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
    affected_days: set[str],
    invalidation_sources: dict[str, set[str]],
) -> None:
    if is_lodging_activity_type(operation.fields.get("type")):
        problems.append(
            _problem(
                "LODGING_OPERATION_REQUIRED",
                "Lodging must be set through SetLodgingSelection.",
                operation,
                "activity",
                operation.activity_id,
            )
        )
        return
    index = _operation_index(plan, operation.op_id, problems)
    if index is None:
        return
    if operation.activity_id in index.activities:
        problems.append(
            _problem(
                "DUPLICATE_ENTITY_ID",
                f"Activity {operation.activity_id!r} already exists.",
                operation,
                "activity",
                operation.activity_id,
            )
        )
        return
    day = index.days.get(operation.day_id)
    if day is None:
        problems.append(
            _problem(
                "UNKNOWN_ENTITY",
                f"Day {operation.day_id!r} does not exist.",
                operation,
                "day",
                operation.day_id,
            )
        )
        return
    if not _validate_fields(
        operation.fields,
        ACTIVITY_MUTABLE_FIELDS,
        ACTIVITY_IDENTITY_FIELDS,
        operation,
        "activity",
        operation.activity_id,
        problems,
    ):
        return
    if (
        "evidence_state" in operation.fields
        and operation.fields["evidence_state"] != "unverified"
    ):
        problems.append(
            _problem(
                "EVIDENCE_STATE_WRITE_FORBIDDEN",
                (
                    "New AI-authored activities may only omit evidence_state "
                    "or initialize it as unverified."
                ),
                operation,
                "activity",
                operation.activity_id,
            )
        )
        return
    location_id = operation.fields.get("location_id")
    if not isinstance(location_id, str) or not location_id.strip():
        problems.append(
            _problem(
                "MISSING_REQUIRED_FIELD",
                "A canonical activity requires a persisted location_id.",
                operation,
                "activity",
                operation.activity_id,
                details={"field": "location_id"},
            )
        )
        return

    places = day["places"]
    insert_at = _placement_index(
        places,
        operation.position,
        operation.anchor_activity_id,
        operation.op_id,
        operation.activity_id,
        problems,
    )
    if insert_at is None:
        return
    activity = _thaw_json(operation.fields)
    activity["activity_id"] = operation.activity_id
    places.insert(insert_at, activity)
    changes.append(
        ChangeRecord(
            op_id=operation.op_id,
            entity_type="activity",
            entity_id=operation.activity_id,
            field="$entity",
            before=None,
            after=activity,
            kind="add",
        )
    )
    affected_days.add(operation.day_id)
    _mark_invalidation(invalidation_sources, operation.day_id, operation.op_id)


def _apply_update_activity(
    plan: dict[str, Any],
    operation: UpdateActivity,
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
    affected_days: set[str],
    invalidation_sources: dict[str, set[str]],
) -> None:
    if is_lodging_activity_type(operation.fields.get("type")):
        problems.append(
            _problem(
                "LODGING_OPERATION_REQUIRED",
                "Lodging must be set through SetLodgingSelection.",
                operation,
                "activity",
                operation.activity_id,
            )
        )
        return
    index = _operation_index(plan, operation.op_id, problems)
    if index is None:
        return
    found = index.activities.get(operation.activity_id)
    if found is None:
        problems.append(
            _problem(
                "UNKNOWN_ENTITY",
                f"Activity {operation.activity_id!r} does not exist.",
                operation,
                "activity",
                operation.activity_id,
            )
        )
        return
    day, _, activity = found
    if not _validate_fields(
        operation.fields,
        ACTIVITY_MUTABLE_FIELDS,
        ACTIVITY_IDENTITY_FIELDS,
        operation,
        "activity",
        operation.activity_id,
        problems,
    ):
        return
    if "evidence_state" in operation.fields:
        problems.append(
            _problem(
                "EVIDENCE_STATE_WRITE_FORBIDDEN",
                (
                    "AI-authored patches cannot directly set evidence_state "
                    "on an existing activity."
                ),
                operation,
                "activity",
                operation.activity_id,
            )
        )
        return

    day_id = _stable_id(day, "day_id")
    invalidates = False
    for field in sorted(operation.fields):
        before = activity.get(field)
        after = _thaw_json(operation.fields[field])
        if _json_equal(before, after):
            continue
        activity[field] = after
        changes.append(
            ChangeRecord(
                op_id=operation.op_id,
                entity_type="activity",
                entity_id=operation.activity_id,
                field=field,
                before=before,
                after=after,
            )
        )
        invalidates = invalidates or field in _ACTIVITY_TRAVEL_FIELDS
    if day_id is not None:
        affected_days.add(day_id)
        if invalidates:
            _mark_invalidation(invalidation_sources, day_id, operation.op_id)


def _apply_place_activity(
    plan: dict[str, Any],
    operation: PlaceActivity,
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
    affected_days: set[str],
    invalidation_sources: dict[str, set[str]],
) -> None:
    index = _operation_index(plan, operation.op_id, problems)
    if index is None:
        return
    found = index.activities.get(operation.activity_id)
    if found is None:
        problems.append(
            _problem(
                "UNKNOWN_ENTITY",
                f"Activity {operation.activity_id!r} does not exist.",
                operation,
                "activity",
                operation.activity_id,
            )
        )
        return
    target_day = index.days.get(operation.day_id)
    if target_day is None:
        problems.append(
            _problem(
                "UNKNOWN_ENTITY",
                f"Day {operation.day_id!r} does not exist.",
                operation,
                "day",
                operation.day_id,
            )
        )
        return
    if operation.anchor_activity_id == operation.activity_id:
        problems.append(
            _problem(
                "INVALID_POSITION",
                "An activity cannot be positioned relative to itself.",
                operation,
                "activity",
                operation.activity_id,
            )
        )
        return

    source_day, source_index, activity = found
    source_day_id = _stable_id(source_day, "day_id")
    source_places = source_day["places"]
    target_places = target_day["places"]
    before_position = {"day_id": source_day_id, "index": source_index}
    old_time = activity.get("time")

    source_places.pop(source_index)
    insert_at = _placement_index(
        target_places,
        operation.position,
        operation.anchor_activity_id,
        operation.op_id,
        operation.activity_id,
        problems,
    )
    if insert_at is None:
        source_places.insert(source_index, activity)
        return
    target_places.insert(insert_at, activity)
    after_position = {"day_id": operation.day_id, "index": insert_at}

    position_changed = not _json_equal(before_position, after_position)
    time_changed = False
    if operation.scheduled_start is not UNSET:
        if operation.scheduled_start is None:
            activity.pop("time", None)
        else:
            activity["time"] = operation.scheduled_start
        new_time = activity.get("time")
        time_changed = not _json_equal(old_time, new_time)
        if time_changed:
            changes.append(
                ChangeRecord(
                    op_id=operation.op_id,
                    entity_type="activity",
                    entity_id=operation.activity_id,
                    field="time",
                    before=old_time,
                    after=new_time,
                    kind="place",
                )
            )
    if position_changed:
        changes.append(
            ChangeRecord(
                op_id=operation.op_id,
                entity_type="activity",
                entity_id=operation.activity_id,
                field="position",
                before=before_position,
                after=after_position,
                kind="place",
            )
        )

    if source_day_id is not None:
        affected_days.add(source_day_id)
    affected_days.add(operation.day_id)
    if position_changed or time_changed:
        if source_day_id is not None:
            _mark_invalidation(
                invalidation_sources, source_day_id, operation.op_id
            )
        _mark_invalidation(invalidation_sources, operation.day_id, operation.op_id)


def _apply_remove_activity(
    plan: dict[str, Any],
    operation: RemoveActivity,
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
    affected_days: set[str],
    invalidation_sources: dict[str, set[str]],
) -> None:
    index = _operation_index(plan, operation.op_id, problems)
    if index is None:
        return
    found = index.activities.get(operation.activity_id)
    if found is None:
        problems.append(
            _problem(
                "UNKNOWN_ENTITY",
                f"Activity {operation.activity_id!r} does not exist.",
                operation,
                "activity",
                operation.activity_id,
            )
        )
        return
    day, position, activity = found
    day_id = _stable_id(day, "day_id")
    day["places"].pop(position)
    changes.append(
        ChangeRecord(
            op_id=operation.op_id,
            entity_type="activity",
            entity_id=operation.activity_id,
            field="$entity",
            before=activity,
            after=None,
            kind="remove",
        )
    )
    if day_id is not None:
        affected_days.add(day_id)
        _mark_invalidation(invalidation_sources, day_id, operation.op_id)


def _apply_update_day(
    plan: dict[str, Any],
    operation: UpdateDay,
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
    affected_days: set[str],
    invalidation_sources: dict[str, set[str]],
) -> None:
    if {"start_location_id", "end_location_id"} & set(operation.fields):
        problems.append(
            _problem(
                "LODGING_OPERATION_REQUIRED",
                (
                    "Day lodging anchors may only be changed by "
                    "SetLodgingSelection."
                ),
                operation,
                "day",
                operation.day_id,
            )
        )
        return
    index = _operation_index(plan, operation.op_id, problems)
    if index is None:
        return
    day = index.days.get(operation.day_id)
    if day is None:
        problems.append(
            _problem(
                "UNKNOWN_ENTITY",
                f"Day {operation.day_id!r} does not exist.",
                operation,
                "day",
                operation.day_id,
            )
        )
        return
    if not _validate_fields(
        operation.fields,
        DAY_MUTABLE_FIELDS,
        DAY_IDENTITY_FIELDS,
        operation,
        "day",
        operation.day_id,
        problems,
    ):
        return
    invalidates = False
    for field in sorted(operation.fields):
        before = day.get(field)
        after = _thaw_json(operation.fields[field])
        if _json_equal(before, after):
            continue
        day[field] = after
        changes.append(
            ChangeRecord(
                op_id=operation.op_id,
                entity_type="day",
                entity_id=operation.day_id,
                field=field,
                before=before,
                after=after,
            )
        )
        invalidates = invalidates or field in _DAY_TRAVEL_FIELDS
    affected_days.add(operation.day_id)
    if invalidates:
        _mark_invalidation(invalidation_sources, operation.day_id, operation.op_id)


def _apply_set_lodging_selection(
    plan: dict[str, Any],
    operation: SetLodgingSelection,
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
    affected_days: set[str],
    invalidation_sources: dict[str, set[str]],
) -> None:
    index = _operation_index(plan, operation.op_id, problems)
    if index is None:
        return

    stays = sorted(
        operation.stays,
        key=lambda item: (item.check_in, item.lodging_id),
    )
    if len({item.lodging_id for item in stays}) != len(stays):
        problems.append(
            _problem(
                "DUPLICATE_LODGING_ID",
                "Lodging selection contains duplicate lodging IDs.",
                operation,
                "lodging",
                None,
            )
        )
        return
    for previous, current in zip(stays, stays[1:]):
        if previous.check_out != current.check_in:
            problems.append(
                _problem(
                    "LODGING_STAY_COVERAGE_INVALID",
                    "Confirmed stays must be contiguous without gaps or overlaps.",
                    operation,
                    "lodging",
                    None,
                )
            )
            return

    anchors = {item.day_id: item for item in operation.anchors}
    if len(anchors) != len(operation.anchors) or any(
        day_id not in index.days for day_id in anchors
    ):
        problems.append(
            _problem(
                "LODGING_ANCHOR_INVALID",
                "Lodging anchors must uniquely reference known days.",
                operation,
                "lodging",
                None,
            )
        )
        return

    stay_by_id = {item.lodging_id: item for item in stays}
    if any(
        (
            anchor.start_lodging_id is not None
            and anchor.start_lodging_id not in stay_by_id
        )
        or (
            anchor.end_lodging_id is not None
            and anchor.end_lodging_id not in stay_by_id
        )
        for anchor in anchors.values()
    ):
        problems.append(
            _problem(
                "LODGING_ANCHOR_INVALID",
                "Lodging anchor references an unknown stay.",
                operation,
                "lodging",
                None,
            )
        )
        return

    by_date: dict[str, str] = {}
    for day_id, day in index.days.items():
        raw_date = day.get("date")
        if not isinstance(raw_date, str):
            continue
        if raw_date in by_date:
            problems.append(
                _problem(
                    "LODGING_ANCHOR_INVALID",
                    "Lodging confirmation requires unique itinerary dates.",
                    operation,
                    "lodging",
                    None,
                )
            )
            return
        by_date[raw_date] = day_id

    expected_end: dict[str, str] = {}
    expected_start: dict[str, str] = {}
    for stay in stays:
        cursor = date.fromisoformat(stay.check_in)
        checkout = date.fromisoformat(stay.check_out)
        while cursor < checkout:
            day_id = by_date.get(cursor.isoformat())
            anchor = anchors.get(day_id) if day_id else None
            if anchor is None or anchor.end_lodging_id != stay.lodging_id:
                problems.append(
                    _problem(
                        "LODGING_ANCHOR_COVERAGE_INVALID",
                        (
                            "Every lodging night requires its day's end "
                            "lodging anchor."
                        ),
                        operation,
                        "lodging",
                        stay.lodging_id,
                    )
                )
                return
            assert day_id is not None
            expected_end[day_id] = stay.lodging_id
            next_date = date.fromordinal(cursor.toordinal() + 1)
            next_day = by_date.get(next_date.isoformat())
            if next_day is not None:
                next_anchor = anchors.get(next_day)
                if (
                    next_anchor is None
                    or next_anchor.start_lodging_id != stay.lodging_id
                ):
                    problems.append(
                        _problem(
                            "LODGING_ANCHOR_COVERAGE_INVALID",
                            (
                                "Every following lodging day requires its "
                                "start lodging anchor."
                            ),
                            operation,
                            "lodging",
                            stay.lodging_id,
                        )
                    )
                    return
                expected_start[next_day] = stay.lodging_id
            cursor = next_date

    first_day_id = by_date.get(stays[0].check_in)
    for day_id, anchor in anchors.items():
        if (
            anchor.end_lodging_id is not None
            and anchor.end_lodging_id != expected_end.get(day_id)
        ):
            problems.append(
                _problem(
                    "LODGING_ANCHOR_COVERAGE_INVALID",
                    "Day end lodging is not the stay covering that night.",
                    operation,
                    "lodging",
                    anchor.end_lodging_id,
                )
            )
            return
        allowed_start = expected_start.get(day_id)
        if (
            day_id == first_day_id
            and anchor.start_lodging_id == stays[0].lodging_id
        ):
            allowed_start = stays[0].lodging_id
        if (
            anchor.start_lodging_id is not None
            and anchor.start_lodging_id != allowed_start
        ):
            problems.append(
                _problem(
                    "LODGING_ANCHOR_COVERAGE_INVALID",
                    (
                        "Day start lodging is not the stay covering the "
                        "preceding night."
                    ),
                    operation,
                    "lodging",
                    anchor.start_lodging_id,
                )
            )
            return

    trip = plan["state"]["trip"]
    assert isinstance(trip, dict)
    before_lodgings = _thaw_json(trip.get("lodgings", []))
    after_lodgings = [
        {
            "lodging_id": stay.lodging_id,
            "location_id": stay.location_id,
            "check_in": stay.check_in,
            "check_out": stay.check_out,
            "kind": stay.kind,
            "decision_state": stay.decision_state,
            "evidence_state": stay.evidence_state,
        }
        for stay in stays
    ]
    if not _json_equal(before_lodgings, after_lodgings):
        trip["lodgings"] = after_lodgings
        changes.append(
            ChangeRecord(
                operation.op_id,
                "lodging",
                "lodgings",
                "lodgings",
                before_lodgings,
                after_lodgings,
                "set",
            )
        )

    for day_id, day in index.days.items():
        anchor = anchors.get(day_id)
        for role in ("start", "end"):
            lodging_id = (
                getattr(anchor, f"{role}_lodging_id")
                if anchor is not None
                else None
            )
            old_id = day.get(f"{role}_lodging_id")
            old_location = day.get(f"{role}_location_id")
            new_location = (
                stay_by_id[lodging_id].location_id
                if lodging_id is not None
                else None
            )
            if lodging_id is None and old_id is None:
                continue
            for field, before, after in (
                (f"{role}_lodging_id", old_id, lodging_id),
                (f"{role}_location_id", old_location, new_location),
            ):
                if _json_equal(before, after):
                    continue
                if after is None:
                    day.pop(field, None)
                else:
                    day[field] = after
                changes.append(
                    ChangeRecord(
                        operation.op_id,
                        "day",
                        day_id,
                        field,
                        before,
                        after,
                        "lodging_anchor",
                    )
                )
                affected_days.add(day_id)
                _mark_invalidation(
                    invalidation_sources,
                    day_id,
                    operation.op_id,
                )


def _apply_add_constraint(
    plan: dict[str, Any],
    operation: AddConstraint,
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
) -> None:
    index = _operation_index(plan, operation.op_id, problems)
    if index is None:
        return
    if operation.constraint_id in index.constraints:
        problems.append(
            _problem(
                "DUPLICATE_ENTITY_ID",
                f"Constraint {operation.constraint_id!r} already exists.",
                operation,
                "constraint",
                operation.constraint_id,
            )
        )
        return
    if not _validate_fields(
        operation.fields,
        CONSTRAINT_MUTABLE_FIELDS,
        CONSTRAINT_IDENTITY_FIELDS,
        operation,
        "constraint",
        operation.constraint_id,
        problems,
    ):
        return
    constraint = _thaw_json(operation.fields)
    constraint["constraint_id"] = operation.constraint_id
    constraints = _constraint_list(plan, create=True)
    if constraints is None:
        problems.append(
            _problem(
                "MALFORMED_PLAN",
                "state.trip.constraints must be an array.",
                operation,
                "constraint",
                operation.constraint_id,
            )
        )
        return
    constraints.append(constraint)
    changes.append(
        ChangeRecord(
            op_id=operation.op_id,
            entity_type="constraint",
            entity_id=operation.constraint_id,
            field="$entity",
            before=None,
            after=constraint,
            kind="add",
        )
    )


def _apply_update_constraint(
    plan: dict[str, Any],
    operation: UpdateConstraint,
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
) -> None:
    index = _operation_index(plan, operation.op_id, problems)
    if index is None:
        return
    found = index.constraints.get(operation.constraint_id)
    if found is None:
        problems.append(
            _problem(
                "UNKNOWN_ENTITY",
                f"Constraint {operation.constraint_id!r} does not exist.",
                operation,
                "constraint",
                operation.constraint_id,
            )
        )
        return
    if not _validate_fields(
        operation.fields,
        CONSTRAINT_MUTABLE_FIELDS,
        CONSTRAINT_IDENTITY_FIELDS,
        operation,
        "constraint",
        operation.constraint_id,
        problems,
    ):
        return
    _, constraint = found
    for field in sorted(operation.fields):
        before = constraint.get(field)
        after = _thaw_json(operation.fields[field])
        if _json_equal(before, after):
            continue
        constraint[field] = after
        changes.append(
            ChangeRecord(
                op_id=operation.op_id,
                entity_type="constraint",
                entity_id=operation.constraint_id,
                field=field,
                before=before,
                after=after,
            )
        )


def _apply_remove_constraint(
    plan: dict[str, Any],
    operation: RemoveConstraint,
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
) -> None:
    index = _operation_index(plan, operation.op_id, problems)
    if index is None:
        return
    found = index.constraints.get(operation.constraint_id)
    if found is None:
        problems.append(
            _problem(
                "UNKNOWN_ENTITY",
                f"Constraint {operation.constraint_id!r} does not exist.",
                operation,
                "constraint",
                operation.constraint_id,
            )
        )
        return
    position, constraint = found
    constraints = _constraint_list(plan, create=False)
    assert constraints is not None
    constraints.pop(position)
    changes.append(
        ChangeRecord(
            op_id=operation.op_id,
            entity_type="constraint",
            entity_id=operation.constraint_id,
            field="$entity",
            before=constraint,
            after=None,
            kind="remove",
        )
    )


def _validate_plan_shape(plan: object) -> MutationProblem | None:
    if not isinstance(plan, dict):
        return MutationProblem(
            code="MALFORMED_PLAN",
            message="Canonical plan root must be an object.",
        )
    state = plan.get("state")
    if not isinstance(state, dict):
        return MutationProblem(
            code="MALFORMED_PLAN",
            message="Canonical plan state must be an object.",
        )
    if not isinstance(state.get("trip"), dict):
        return MutationProblem(
            code="MALFORMED_PLAN",
            message="Canonical plan state.trip must be an object.",
        )
    itinerary = state.get("itinerary")
    if not isinstance(itinerary, dict):
        return MutationProblem(
            code="MALFORMED_PLAN",
            message="Canonical plan state.itinerary must be an object.",
        )
    if not isinstance(itinerary.get("days"), list):
        return MutationProblem(
            code="MALFORMED_PLAN",
            message="Canonical plan state.itinerary.days must be an array.",
        )
    return None


def _build_index(
    plan: dict[str, Any],
) -> tuple[_EntityIndex | None, list[MutationProblem]]:
    problems: list[MutationProblem] = []
    shape_problem = _validate_plan_shape(plan)
    if shape_problem is not None:
        return None, [shape_problem]
    days_list = plan["state"]["itinerary"]["days"]
    days: dict[str, dict[str, Any]] = {}
    activities: dict[str, tuple[dict[str, Any], int, dict[str, Any]]] = {}
    for day in days_list:
        if not isinstance(day, dict):
            problems.append(
                MutationProblem(
                    code="MALFORMED_PLAN",
                    message="Each itinerary day must be an object.",
                )
            )
            continue
        day_id = _stable_id(day, "day_id")
        if day_id is None:
            problems.append(
                MutationProblem(
                    code="MISSING_STABLE_ID",
                    message="Every canonical day must have a persisted day_id.",
                    entity_type="day",
                )
            )
            continue
        if day_id in days:
            problems.append(
                MutationProblem(
                    code="DUPLICATE_ENTITY_ID",
                    message=f"Duplicate day ID {day_id!r}.",
                    entity_type="day",
                    entity_id=day_id,
                )
            )
            continue
        places = day.get("places")
        if not isinstance(places, list):
            problems.append(
                MutationProblem(
                    code="MALFORMED_PLAN",
                    message=f"Day {day_id!r} places must be an array.",
                    entity_type="day",
                    entity_id=day_id,
                )
            )
            continue
        if "travel" in day and not isinstance(day["travel"], list):
            problems.append(
                MutationProblem(
                    code="MALFORMED_PLAN",
                    message=f"Day {day_id!r} travel must be an array.",
                    entity_type="day",
                    entity_id=day_id,
                )
            )
        days[day_id] = day
        for position, activity in enumerate(places):
            if not isinstance(activity, dict):
                problems.append(
                    MutationProblem(
                        code="MALFORMED_PLAN",
                        message=f"Day {day_id!r} contains a non-object activity.",
                        entity_type="day",
                        entity_id=day_id,
                    )
                )
                continue
            activity_id = _stable_id(activity, "activity_id")
            if activity_id is None:
                problems.append(
                    MutationProblem(
                        code="MISSING_STABLE_ID",
                        message="Every canonical activity must have activity_id.",
                        entity_type="activity",
                    )
                )
                continue
            if activity_id in activities:
                problems.append(
                    MutationProblem(
                        code="DUPLICATE_ENTITY_ID",
                        message=f"Duplicate activity ID {activity_id!r}.",
                        entity_type="activity",
                        entity_id=activity_id,
                    )
                )
                continue
            activities[activity_id] = (day, position, activity)

    constraints: dict[str, tuple[int, dict[str, Any]]] = {}
    raw_constraints = _constraint_list(plan, create=False)
    if raw_constraints is None:
        problems.append(
            MutationProblem(
                code="MALFORMED_PLAN",
                message="state.trip.constraints must be an array when present.",
                entity_type="constraint",
            )
        )
    else:
        for position, constraint in enumerate(raw_constraints):
            if not isinstance(constraint, dict):
                problems.append(
                    MutationProblem(
                        code="MALFORMED_PLAN",
                        message="Every constraint must be an object.",
                        entity_type="constraint",
                    )
                )
                continue
            constraint_id = _stable_id(constraint, "constraint_id")
            if constraint_id is None:
                problems.append(
                    MutationProblem(
                        code="MISSING_STABLE_ID",
                        message="Every canonical constraint must have constraint_id.",
                        entity_type="constraint",
                    )
                )
                continue
            if constraint_id in constraints:
                problems.append(
                    MutationProblem(
                        code="DUPLICATE_ENTITY_ID",
                        message=f"Duplicate constraint ID {constraint_id!r}.",
                        entity_type="constraint",
                        entity_id=constraint_id,
                    )
                )
                continue
            constraints[constraint_id] = (position, constraint)
    return _EntityIndex(days, activities, constraints), problems


def _operation_index(
    plan: dict[str, Any],
    op_id: str,
    problems: list[MutationProblem],
) -> _EntityIndex | None:
    index, new_problems = _build_index(plan)
    if new_problems:
        for problem in new_problems:
            problems.append(
                MutationProblem(
                    code=problem.code,
                    message=problem.message,
                    op_id=op_id,
                    entity_type=problem.entity_type,
                    entity_id=problem.entity_id,
                    details=problem.details,
                )
            )
        return None
    return index


def _constraint_list(
    plan: dict[str, Any], *, create: bool
) -> list[dict[str, Any]] | None:
    try:
        trip = plan["state"]["trip"]
    except (KeyError, TypeError):
        return None
    if "constraints" not in trip or trip["constraints"] is None:
        if not create:
            return []
        trip["constraints"] = []
    constraints = trip["constraints"]
    return constraints if isinstance(constraints, list) else None


def _validate_constraint_references(
    plan: dict[str, Any], problems: list[MutationProblem]
) -> None:
    index, index_problems = _build_index(plan)
    if index is None or index_problems:
        return
    known_ids = set(index.days) | set(index.activities)
    for constraint_id, (_, constraint) in index.constraints.items():
        subjects = constraint.get("subject_ids", [])
        if not isinstance(subjects, list):
            problems.append(
                MutationProblem(
                    code="MALFORMED_REFERENCE",
                    message=(
                        f"Constraint {constraint_id!r} subject_ids must be an array."
                    ),
                    entity_type="constraint",
                    entity_id=constraint_id,
                )
            )
            continue
        dangling = [
            subject
            for subject in subjects
            if not isinstance(subject, str) or subject not in known_ids
        ]
        dangling.sort(key=lambda value: _canonical_json(_thaw_json(value)))
        if dangling:
            problems.append(
                MutationProblem(
                    code="DEPENDENT_REFERENCE",
                    message=(
                        f"Constraint {constraint_id!r} contains unknown subjects."
                    ),
                    entity_type="constraint",
                    entity_id=constraint_id,
                    details={"subject_ids": dangling},
                )
            )


def _placement_index(
    places: list[dict[str, Any]],
    position: Placement,
    anchor_activity_id: str | None,
    op_id: str,
    entity_id: str,
    problems: list[MutationProblem],
) -> int | None:
    if position in (Placement.START, Placement.END):
        if anchor_activity_id is not None:
            problems.append(
                MutationProblem(
                    code="INVALID_POSITION",
                    message=f"Placement {position.value!r} cannot have an anchor.",
                    op_id=op_id,
                    entity_type="activity",
                    entity_id=entity_id,
                )
            )
            return None
        return 0 if position is Placement.START else len(places)
    if anchor_activity_id is None:
        problems.append(
            MutationProblem(
                code="INVALID_POSITION",
                message=f"Placement {position.value!r} requires an activity anchor.",
                op_id=op_id,
                entity_type="activity",
                entity_id=entity_id,
            )
        )
        return None
    for index, activity in enumerate(places):
        if _stable_id(activity, "activity_id") == anchor_activity_id:
            return index if position is Placement.BEFORE else index + 1
    problems.append(
        MutationProblem(
            code="UNKNOWN_POSITION_ANCHOR",
            message=f"Activity anchor {anchor_activity_id!r} does not exist in the day.",
            op_id=op_id,
            entity_type="activity",
            entity_id=entity_id,
            details={"anchor_activity_id": anchor_activity_id},
        )
    )
    return None


def _net_travel_invalidation_sources(
    original: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, set[str]]:
    """Return days whose persisted route assumptions changed in the net diff.

    Operation-by-operation invalidation is overly conservative for an atomic
    patch: an activity changed from A to B and back to A never exposes B as a
    committed state.  Deriving invalidation from both endpoint snapshots also
    keeps add-then-remove and move-then-restore patches true no-ops.
    """

    original_index, original_problems = _build_index(original)
    candidate_index, candidate_problems = _build_index(candidate)
    if (
        original_index is None
        or candidate_index is None
        or original_problems
        or candidate_problems
    ):
        return {}

    invalidated: set[str] = set()
    for day_id in set(original_index.days) | set(candidate_index.days):
        before_day = original_index.days.get(day_id)
        after_day = candidate_index.days.get(day_id)
        if before_day is None or after_day is None:
            invalidated.add(day_id)
            continue
        if any(
            not _json_equal(before_day.get(field), after_day.get(field))
            for field in _DAY_TRAVEL_FIELDS
        ):
            invalidated.add(day_id)
        before_order = [
            _stable_id(activity, "activity_id")
            for activity in before_day["places"]
        ]
        after_order = [
            _stable_id(activity, "activity_id")
            for activity in after_day["places"]
        ]
        if not _json_equal(before_order, after_order):
            invalidated.add(day_id)

    for activity_id in set(original_index.activities) | set(
        candidate_index.activities
    ):
        before_found = original_index.activities.get(activity_id)
        after_found = candidate_index.activities.get(activity_id)
        if before_found is None:
            if after_found is not None:
                after_day_id = _stable_id(after_found[0], "day_id")
                if after_day_id is not None:
                    invalidated.add(after_day_id)
            continue
        if after_found is None:
            before_day_id = _stable_id(before_found[0], "day_id")
            if before_day_id is not None:
                invalidated.add(before_day_id)
            continue

        before_day_id = _stable_id(before_found[0], "day_id")
        after_day_id = _stable_id(after_found[0], "day_id")
        if before_day_id != after_day_id:
            if before_day_id is not None:
                invalidated.add(before_day_id)
            if after_day_id is not None:
                invalidated.add(after_day_id)
            continue
        if before_day_id is not None and any(
            not _json_equal(
                before_found[2].get(field),
                after_found[2].get(field),
            )
            for field in _ACTIVITY_TRAVEL_FIELDS
        ):
            invalidated.add(before_day_id)

    return {day_id: {"net-diff"} for day_id in invalidated}


def _invalidate_day_travel(
    plan: dict[str, Any],
    invalidation_sources: Mapping[str, set[str]],
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
) -> None:
    index, index_problems = _build_index(plan)
    if index is None or index_problems:
        return
    for day_id in sorted(invalidation_sources):
        day = index.days.get(day_id)
        if day is None:
            continue
        before = day.get("travel")
        if before is None or before == []:
            continue
        if not isinstance(before, list):
            problems.append(
                MutationProblem(
                    code="MALFORMED_PLAN",
                    message=f"Cannot invalidate malformed travel for day {day_id!r}.",
                    entity_type="day",
                    entity_id=day_id,
                )
            )
            continue
        day["travel"] = []
        changes.append(
            ChangeRecord(
                op_id="derived",
                entity_type="day",
                entity_id=day_id,
                field="travel",
                before=before,
                after=[],
                kind="invalidate",
            )
        )


def _synchronize_migration_metadata(
    plan: dict[str, Any],
    *,
    invalidated_day_ids: frozenset[str],
    problems: list[MutationProblem],
    changes: list[ChangeRecord],
) -> None:
    """Keep revisioned legacy-safety metadata valid after semantic changes."""

    trip = plan.get("state", {}).get("trip")
    if not isinstance(trip, dict):
        return
    metadata = trip.get(_MIGRATION_META_KEY)
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        problems.append(
            MutationProblem(
                code="MALFORMED_PLAN",
                message=f"state.trip.{_MIGRATION_META_KEY} must be an object.",
                entity_type="trip",
                entity_id=str(plan.get("trip_id", "")) or None,
            )
        )
        return
    migration = metadata.get("migration")
    if migration is None:
        return
    if not isinstance(migration, dict):
        problems.append(
            MutationProblem(
                code="MALFORMED_PLAN",
                message=(
                    f"state.trip.{_MIGRATION_META_KEY}.migration must be an object."
                ),
                entity_type="trip",
                entity_id=str(plan.get("trip_id", "")) or None,
            )
        )
        return

    index, index_problems = _build_index(plan)
    if index is None or index_problems:
        return
    current_activity_ids = set(index.activities)
    protected = migration.get("protected_activity_ids", [])
    if not isinstance(protected, list) or any(
        not isinstance(activity_id, str) for activity_id in protected
    ):
        problems.append(
            MutationProblem(
                code="MALFORMED_PLAN",
                message="migration.protected_activity_ids must be an array of IDs.",
                entity_type="trip",
                entity_id=str(plan.get("trip_id", "")) or None,
            )
        )
    else:
        remaining = [
            activity_id
            for activity_id in protected
            if activity_id in current_activity_ids
        ]
        if remaining != protected:
            migration["protected_activity_ids"] = remaining
            changes.append(
                ChangeRecord(
                    op_id="derived",
                    entity_type="trip",
                    entity_id=str(plan.get("trip_id", "")) or "trip",
                    field=(
                        f"{_MIGRATION_META_KEY}.migration."
                        "protected_activity_ids"
                    ),
                    before=protected,
                    after=remaining,
                    kind="metadata_cleanup",
                )
            )

    fragments = migration.get("ignored_travel_edges", [])
    if not isinstance(fragments, list) or any(
        not isinstance(fragment, dict) for fragment in fragments
    ):
        problems.append(
            MutationProblem(
                code="MALFORMED_PLAN",
                message="migration.ignored_travel_edges must be an array of objects.",
                entity_type="trip",
                entity_id=str(plan.get("trip_id", "")) or None,
            )
        )
    elif invalidated_day_ids:
        remaining_fragments = [
            fragment
            for fragment in fragments
            if fragment.get("day_id") not in invalidated_day_ids
        ]
        if remaining_fragments != fragments:
            migration["ignored_travel_edges"] = remaining_fragments
            changes.append(
                ChangeRecord(
                    op_id="derived",
                    entity_type="trip",
                    entity_id=str(plan.get("trip_id", "")) or "trip",
                    field=(
                        f"{_MIGRATION_META_KEY}.migration."
                        "ignored_travel_edges"
                    ),
                    before=fragments,
                    after=remaining_fragments,
                    kind="invalidate",
                )
            )


def _migration_protected_activity_ids(plan: Mapping[str, Any]) -> frozenset[str]:
    try:
        protected = plan["state"]["trip"][_MIGRATION_META_KEY]["migration"][
            "protected_activity_ids"
        ]
    except (KeyError, TypeError):
        return frozenset()
    if not isinstance(protected, list):
        return frozenset()
    return frozenset(
        activity_id
        for activity_id in protected
        if isinstance(activity_id, str) and activity_id
    )


def _protected_net_changes(
    original: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[ChangeRecord, ...]:
    original_index, original_problems = _build_index(original)
    final_index, final_problems = _build_index(candidate)
    if (
        original_index is None
        or final_index is None
        or original_problems
        or final_problems
    ):
        return ()

    protected: list[ChangeRecord] = []
    metadata_protected_ids = _migration_protected_activity_ids(original)
    for activity_id in sorted(original_index.activities):
        original_day, original_position, original_activity = (
            original_index.activities[activity_id]
        )
        if not _is_protected_or_unclassified(
            original_activity,
            metadata_protected=activity_id in metadata_protected_ids,
        ):
            continue
        original_day_id = _stable_id(original_day, "day_id")
        final_found = final_index.activities.get(activity_id)
        if final_found is None:
            protected.append(
                ChangeRecord(
                    op_id="approval-policy",
                    entity_type="activity",
                    entity_id=activity_id,
                    field="$entity",
                    before=original_activity,
                    after=None,
                    kind="protected_remove",
                )
            )
            continue

        final_day, final_position, final_activity = final_found
        final_day_id = _stable_id(final_day, "day_id")
        if original_day_id != final_day_id:
            protected.append(
                _protected_change(
                    activity_id, "day_id", original_day_id, final_day_id
                )
            )
        if (
            original_day_id != final_day_id
            or original_position != final_position
        ):
            protected.append(
                _protected_change(
                    activity_id,
                    "position",
                    {"day_id": original_day_id, "index": original_position},
                    {"day_id": final_day_id, "index": final_position},
                )
            )

        for field in (
            "time",
            "duration_min",
            "allowed_windows",
            "evidence_state",
        ):
            before = original_activity.get(field)
            after = final_activity.get(field)
            if not _json_equal(before, after):
                protected.append(
                    _protected_change(activity_id, field, before, after)
                )

        before_location = _location_identity(original_activity)
        after_location = _location_identity(final_activity)
        if not _json_equal(before_location, after_location):
            protected.append(
                _protected_change(
                    activity_id, "location", before_location, after_location
                )
            )

        if _classification_downgraded(
            original_activity,
            final_activity,
            field="decision_state",
            ranks=_DECISION_RANK,
        ):
            protected.append(
                _protected_change(
                    activity_id,
                    "decision_state",
                    _field_marker(original_activity, "decision_state"),
                    _field_marker(final_activity, "decision_state"),
                )
            )
        if _classification_downgraded(
            original_activity,
            final_activity,
            field="flexibility",
            ranks=_FLEXIBILITY_RANK,
        ):
            protected.append(
                _protected_change(
                    activity_id,
                    "flexibility",
                    _field_marker(original_activity, "flexibility"),
                    _field_marker(final_activity, "flexibility"),
                )
            )

        if original_day_id is not None and final_day_id == original_day_id:
            for field in (
                "allowed_modes",
                "available_end",
                "available_start",
                "date",
                "day",
                "end_location_id",
                "start_location_id",
                "timezone",
            ):
                before = original_day.get(field)
                after = final_day.get(field)
                if not _json_equal(before, after):
                    protected.append(
                        _protected_change(
                            activity_id,
                            f"day.{field}",
                            before,
                            after,
                        )
                    )

    final_metadata_protected_ids = _migration_protected_activity_ids(
        candidate
    )
    adopted_ids = metadata_protected_ids.difference(
        final_metadata_protected_ids
    )
    for activity_id in sorted(adopted_ids):
        final_found = final_index.activities.get(activity_id)
        if final_found is None:
            continue
        final_activity = final_found[2]
        protected.append(
            ChangeRecord(
                op_id="approval-policy",
                entity_type="activity",
                entity_id=activity_id,
                field="migration.protection",
                before={"unclassified": True},
                after={
                    "decision_state": _field_marker(
                        final_activity,
                        "decision_state",
                    ),
                    "flexibility": _field_marker(
                        final_activity,
                        "flexibility",
                    ),
                },
                kind="baseline_adoption",
            )
        )

    protected.extend(hard_constraint_protected_changes(original, candidate))
    return tuple(
        sorted(
            protected,
            key=lambda change: (
                change.entity_type,
                change.entity_id,
                change.field,
                _canonical_json(_thaw_json(change.before)),
                _canonical_json(_thaw_json(change.after)),
            ),
        )
    )


def hard_constraint_protected_changes(
    original: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    protect_target_hard: bool = False,
) -> tuple[ChangeRecord, ...]:
    """Return exact net changes involving authority-owned hard constraints.

    Ordinary mutation protects constraints that were hard in the observed
    base. Rollback additionally sets ``protect_target_hard`` so restoring or
    removing a hard constraint cannot drift outside the same approval policy.
    """

    try:
        original_plan = _thaw_json(original)
        candidate_plan = _thaw_json(candidate)
    except (TypeError, ValueError):
        return ()
    if not isinstance(original_plan, dict) or not isinstance(
        candidate_plan, dict
    ):
        return ()
    original_index, original_problems = _build_index(original_plan)
    final_index, final_problems = _build_index(candidate_plan)
    if (
        original_index is None
        or final_index is None
        or original_problems
        or final_problems
    ):
        return ()

    changes: list[ChangeRecord] = []
    all_ids = set(original_index.constraints) | set(final_index.constraints)
    for constraint_id in sorted(all_ids):
        original_found = original_index.constraints.get(constraint_id)
        final_found = final_index.constraints.get(constraint_id)
        original_constraint = (
            original_found[1] if original_found is not None else None
        )
        final_constraint = (
            final_found[1] if final_found is not None else None
        )
        if not (
            _constraint_is_hard(original_constraint)
            or (
                protect_target_hard
                and _constraint_is_hard(final_constraint)
            )
        ):
            continue
        if original_constraint is None or final_constraint is None:
            changes.append(
                ChangeRecord(
                    op_id="approval-policy",
                    entity_type="constraint",
                    entity_id=constraint_id,
                    field="$entity",
                    before=original_constraint,
                    after=final_constraint,
                    kind=(
                        "protected_constraint_add"
                        if original_constraint is None
                        else "protected_constraint_remove"
                    ),
                )
            )
            continue
        for field in sorted(CONSTRAINT_MUTABLE_FIELDS):
            before = _constraint_semantic_field(
                original_constraint, field
            )
            after = _constraint_semantic_field(final_constraint, field)
            if _json_equal(before, after):
                continue
            changes.append(
                ChangeRecord(
                    op_id="approval-policy",
                    entity_type="constraint",
                    entity_id=constraint_id,
                    field=field,
                    before=before,
                    after=after,
                    kind="protected_constraint_change",
                )
            )
    return tuple(
        sorted(
            changes,
            key=lambda change: (
                change.entity_type,
                change.entity_id,
                change.field,
                _canonical_json(_thaw_json(change.before)),
                _canonical_json(_thaw_json(change.after)),
            ),
        )
    )


def _constraint_is_hard(
    constraint: Mapping[str, Any] | None,
) -> bool:
    if constraint is None:
        return False
    return constraint.get("strength", "hard") in (None, "hard")


def _constraint_semantic_field(
    constraint: Mapping[str, Any],
    field: str,
) -> Any:
    defaults: dict[str, Any] = {
        "confidence": 1.0,
        "origin": "user",
        "params": {},
        "source_text": None,
        "strength": "hard",
        "subject_ids": [],
    }
    if field in defaults:
        value = constraint.get(field, defaults[field])
        if field == "strength" and value is None:
            value = "hard"
        return _thaw_json(value)
    if field not in constraint:
        return {"unclassified": True}
    return _thaw_json(constraint[field])


def _is_protected_or_unclassified(
    activity: dict[str, Any], *, metadata_protected: bool = False
) -> bool:
    if metadata_protected:
        return True
    if "decision_state" not in activity or "flexibility" not in activity:
        return True
    decision = activity.get("decision_state")
    flexibility = activity.get("flexibility")
    if decision not in _DECISION_RANK or flexibility not in _FLEXIBILITY_RANK:
        return True
    return (
        decision in _PROTECTED_DECISIONS
        or flexibility in _PROTECTED_FLEXIBILITY
    )


def _classification_downgraded(
    original: dict[str, Any],
    final: dict[str, Any],
    *,
    field: str,
    ranks: Mapping[str, int],
) -> bool:
    original_present = field in original
    final_present = field in final
    if not original_present:
        return final_present and final.get(field) not in {
            key for key, rank in ranks.items() if rank == max(ranks.values())
        }
    if not final_present:
        return True
    before = original.get(field)
    after = final.get(field)
    if before == after:
        return False
    if before not in ranks or after not in ranks:
        return True
    return ranks[after] < ranks[before]


def _location_identity(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        field: _thaw_json(activity.get(field))
        for field in ("location_id", "place_id", "lat", "lng", "maps_query")
    }


def _field_marker(value: Mapping[str, Any], field: str) -> Any:
    if field not in value:
        return {"unclassified": True}
    return _thaw_json(value[field])


def _protected_change(
    activity_id: str, field: str, before: Any, after: Any
) -> ChangeRecord:
    return ChangeRecord(
        op_id="approval-policy",
        entity_type="activity",
        entity_id=activity_id,
        field=field,
        before=before,
        after=after,
        kind="protected_change",
    )


def _validate_fields(
    fields: Mapping[str, Any],
    allowed: frozenset[str],
    identity: frozenset[str],
    operation: PatchOperation,
    entity_type: str,
    entity_id: str,
    problems: list[MutationProblem],
) -> bool:
    valid = True
    for field in sorted(fields):
        if field in identity:
            problems.append(
                _problem(
                    "IDENTITY_FIELD_FORBIDDEN",
                    f"Field {field!r} is identity or placement data and cannot "
                    "be changed by this operation.",
                    operation,
                    entity_type,
                    entity_id,
                    details={"field": field},
                )
            )
            valid = False
        elif field not in allowed:
            problems.append(
                _problem(
                    "FIELD_NOT_MUTABLE",
                    f"Field {field!r} is not in the semantic mutation whitelist.",
                    operation,
                    entity_type,
                    entity_id,
                    details={"field": field},
                )
            )
            valid = False
    return valid


def _downgrade_changed_activity_evidence(
    original: dict[str, Any],
    candidate: dict[str, Any],
    changes: list[ChangeRecord],
    problems: list[MutationProblem],
) -> None:
    """Invalidate authority-owned evidence after a net fact change.

    The downgrade is derived from the final net diff, not operation order.
    A fact changed and restored within one patch therefore preserves verified
    evidence, while any persisted new fact becomes explicitly unverified.
    """

    original_index, original_problems = _build_index(original)
    final_index, final_problems = _build_index(candidate)
    if (
        original_index is None
        or final_index is None
        or original_problems
        or final_problems
    ):
        return
    for activity_id in sorted(
        set(original_index.activities).intersection(
            final_index.activities
        )
    ):
        original_activity = original_index.activities[activity_id][2]
        final_activity = final_index.activities[activity_id][2]
        fact_changed = any(
            not _json_equal(
                original_activity.get(field),
                final_activity.get(field),
            )
            for field in sorted(_ACTIVITY_EVIDENCE_FIELDS)
        )
        if not fact_changed:
            if not _json_equal(
                original_activity.get("evidence_state"),
                final_activity.get("evidence_state"),
            ):
                problems.append(
                    MutationProblem(
                        code="EVIDENCE_STATE_WRITE_FORBIDDEN",
                        message=(
                            "AI-authored patches cannot indirectly replace "
                            "evidence_state on an existing activity."
                        ),
                        entity_type="activity",
                        entity_id=activity_id,
                    )
                )
            continue
        before = original_activity.get("evidence_state")
        final_activity["evidence_state"] = "unverified"
        if before == "unverified":
            continue
        changes.append(
            ChangeRecord(
                op_id="derived",
                entity_type="activity",
                entity_id=activity_id,
                field="evidence_state",
                before=before,
                after="unverified",
                kind="invalidate_evidence",
            )
        )


def _mark_invalidation(
    sources: dict[str, set[str]], day_id: str, op_id: str
) -> None:
    sources.setdefault(day_id, set()).add(op_id)


def _problem(
    code: str,
    message: str,
    operation: PatchOperation,
    entity_type: str,
    entity_id: str,
    *,
    details: JsonMapping = MappingProxyType({}),
) -> MutationProblem:
    return MutationProblem(
        code=code,
        message=message,
        op_id=operation.op_id,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )


def _deduplicate_problems(
    problems: Sequence[MutationProblem],
) -> tuple[MutationProblem, ...]:
    result: list[MutationProblem] = []
    seen: set[str] = set()
    for problem in problems:
        key = _canonical_json(
            {
                "code": problem.code,
                "message": problem.message,
                "op_id": problem.op_id,
                "entity_type": problem.entity_type,
                "entity_id": problem.entity_id,
                "details": _thaw_json(problem.details),
            }
        )
        if key not in seen:
            result.append(problem)
            seen.add(key)
    return tuple(result)


def _stable_id(value: Mapping[str, Any], primary: str) -> str | None:
    candidate = value.get(primary, value.get("id"))
    return candidate if isinstance(candidate, str) and candidate.strip() else None


def _add_anchor(value: dict[str, Any], anchor: str | None) -> None:
    if anchor is not None:
        value["anchor_activity_id"] = anchor


def _coerce_placement(value: Placement | str) -> Placement:
    if isinstance(value, Placement):
        return value
    if isinstance(value, str):
        try:
            return Placement(value)
        except ValueError as exc:
            raise ValueError(f"Unknown placement {value!r}") from exc
    raise TypeError("position must be a Placement")


def _duplicates(values: Sequence[str] | Any) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_request_id(
    value: object,
    name: str,
    *,
    max_length: int = _MAX_REQUEST_ID_LENGTH,
) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a non-empty string")
    if (
        not value
        or len(value) > max_length
        or value != value.strip()
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise ValueError(
            f"{name} must be 1-{max_length} visible characters without "
            "leading/trailing whitespace or control characters"
        )


def _optional_request_id(value: object, name: str) -> None:
    if value is not None:
        _require_request_id(value, name)


def _optional_text(value: object, name: str) -> None:
    if value is not None:
        _require_text(value, name)


def _require_sha256_digest(
    value: object,
    name: str,
    *,
    prefixed: bool,
) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    if prefixed != value.startswith("sha256:"):
        prefix = "prefixed" if prefixed else "unprefixed"
        raise ValueError(f"{name} must be a {prefix} SHA-256 digest")


def _require_canonical_lodging_location_id(
    value: object,
    name: str,
) -> None:
    if (
        not isinstance(value, str)
        or _CANONICAL_LODGING_LOCATION_RE.fullmatch(value) is None
    ):
        raise ValueError(
            f"{name} must be a privacy-safe canonical lodging location ID"
        )


def _aware_utc_datetime(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def is_lodging_activity_type(value: object) -> bool:
    """Return whether a legacy activity type denotes canonical lodging."""

    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized in _LODGING_ACTIVITY_TYPES


def _lodging_date(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be an ISO date")


def _freeze_mapping(value: object, name: str) -> JsonMapping:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    frozen = _freeze_json(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numeric values must be finite")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = _freeze_json(item)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"Value of type {type(value).__name__!r} is not JSON-compatible")


def _thaw_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numeric values must be finite")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = _thaw_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    raise TypeError(f"Value of type {type(value).__name__!r} is not JSON-compatible")


def _json_equal(left: Any, right: Any) -> bool:
    try:
        return _canonical_json(_thaw_json(left)) == _canonical_json(
            _thaw_json(right)
        )
    except (TypeError, ValueError):
        return left == right


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    encoded = _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "ACTIVITY_IDENTITY_FIELDS",
    "ACTIVITY_MUTABLE_FIELDS",
    "AdoptMigratedBaseline",
    "AddActivity",
    "AddConstraint",
    "ApprovalGrant",
    "ConfirmedLodgingStay",
    "ChangeRecord",
    "CONSTRAINT_IDENTITY_FIELDS",
    "CONSTRAINT_MUTABLE_FIELDS",
    "DAY_IDENTITY_FIELDS",
    "DAY_MUTABLE_FIELDS",
    "MutationProblem",
    "MigratedActivityClassification",
    "MigratedActivityClassificationKind",
    "LodgingAnchorAssignment",
    "LodgingConfirmationGrant",
    "PATCH_VERSION",
    "PatchDraft",
    "PatchOperation",
    "Placement",
    "PlaceActivity",
    "PlanPatch",
    "RemoveActivity",
    "RemoveConstraint",
    "SetLodgingSelection",
    "UNSET",
    "UpdateActivity",
    "UpdateConstraint",
    "UpdateDay",
    "apply_patch_to_plan",
    "apply_plan_patch",
    "approval_scope_digest",
    "build_signed_lodging_confirmation_grant",
    "hard_constraint_protected_changes",
    "is_lodging_activity_type",
    "lodging_confirmation_grant_payload",
    "lodging_confirmation_scope_digest",
    "patch_digest",
    "patch_to_dict",
    "preview_patch",
]
