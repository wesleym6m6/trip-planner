"""Storage-agnostic contracts for a bounded AI itinerary repair loop.

The model-facing boundary in this module is deliberately smaller than
``PlanPatch``.  A model may propose semantic operations and explain which
structured issues they address; a trusted binder injects trip identity,
revision, and idempotency.  Approvals, receipts, filesystem paths, and claimed
validation outcomes never cross the model boundary.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from .codec import (
    FrozenJsonValue,
    PlanCodecError,
    canonical_json_bytes,
    decode_json_bytes,
    deep_copy_json,
    freeze_json,
    plan_to_trip_state,
)
from .composition import EvidenceBinding, compose_trip_state
from .facts import EvidenceSnapshot
from .models import (
    CheckIssue,
    CheckReport,
    IssueSeverity,
)
from .mutations import (
    ACTIVITY_MUTABLE_FIELDS,
    CONSTRAINT_MUTABLE_FIELDS,
    DAY_MUTABLE_FIELDS,
    AddActivity,
    AddConstraint,
    PatchOperation,
    PlaceActivity,
    Placement,
    PlanPatch,
    RemoveActivity,
    RemoveConstraint,
    UNSET,
    UpdateActivity,
    UpdateConstraint,
    UpdateDay,
    patch_to_dict,
)
from .timeline import evaluate_timeline
from .scheduling import trip_state_digest


SNAPSHOT_VERSION = "planner-snapshot/v1"
PROPOSAL_VERSION = "repair-proposal/v1"
_MAX_ID_LENGTH = 256
_MAX_REASON_LENGTH = 4096
_MAX_SUMMARY_LENGTH = 4096
_MAX_PROPOSAL_BYTES = 1024 * 1024
_MAX_PROPOSAL_OPERATIONS = 128
_ISSUE_ID_RE = re.compile(r"^issue-[0-9a-f]{32}$")
_AI_CONSTRAINT_MUTABLE_FIELDS = CONSTRAINT_MUTABLE_FIELDS - {"origin"}
_ISSUE_SCOPE_DETAIL_KEYS = frozenset(
    {
        "activity_day_id",
        "constraint_id",
        "day_end",
        "day_id",
        "day_start",
        "first_day_id",
        "following_day_id",
        "following_start_location_id",
        "from_date",
        "from_index",
        "from_location_id",
        "inbound_day_id",
        "local_datetime",
        "missing",
        "mode",
        "previous_day_id",
        "previous_end_location_id",
        "recommended_mode",
        "return_day_id",
        "second_day_id",
        "timezone",
        "to_date",
        "to_index",
        "to_location_id",
        "trip_end",
        "trip_start",
    }
)
_PLACEMENT_OPTION_KEYS = frozenset(
    {
        "assign_activity_to_day",
        "change_previous_activity",
        "move_activity",
        "move_activity_to_allowed_day",
        "move_activity_to_another_day",
        "move_last_activity",
        "reorder_activities",
        "schedule_constraint_subjects",
        "schedule_dependencies",
        "schedule_required_activity",
    }
)
_EXPLICIT_TIME_OPTION_KEYS = frozenset(
    {"move_activity_outside_dst_gap", "restore_fixed_time"}
)
_INTERDAY_ACTIVITY_FIELDS = frozenset(
    {
        "allowed_windows",
        "display_name",
        "duration_min",
        "evidence_state",
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


class RepairContractError(ValueError):
    """Machine-readable failure at the AI repair contract boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
    ) -> None:
        self.code = code
        self.path = path
        location = f" ({path})" if path else ""
        super().__init__(f"{code}: {message}{location}")


class IssueOwner(str, Enum):
    """The safest primary actor for resolving an issue or repair option."""

    PLANNER = "planner"
    PROVIDER = "provider"
    HUMAN = "human"
    SYSTEM = "system"


class RepairOptionKind(str, Enum):
    """Small explicit vocabulary; this is not a general action DSL."""

    PLAN_PATCH = "plan_patch"
    PROVIDER_QUERY = "provider_query"
    USER_DECISION = "user_decision"
    SYSTEM_ACTION = "system_action"


@dataclass(frozen=True, slots=True)
class RepairOption:
    """One typed interpretation of an existing ``suggested_fixes`` key."""

    key: str
    kind: RepairOptionKind
    owner: IssueOwner
    auto_allowed: bool
    requires_evidence: bool = False
    requires_approval: bool = False
    blocking: bool = False
    source_fix: str | None = None

    def __post_init__(self) -> None:
        _require_visible_id(self.key, "RepairOption.key")
        if not isinstance(self.kind, RepairOptionKind):
            raise TypeError("RepairOption.kind must be RepairOptionKind")
        if not isinstance(self.owner, IssueOwner):
            raise TypeError("RepairOption.owner must be IssueOwner")
        for value, name in (
            (self.auto_allowed, "auto_allowed"),
            (self.requires_evidence, "requires_evidence"),
            (self.requires_approval, "requires_approval"),
            (self.blocking, "blocking"),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"RepairOption.{name} must be bool")
        if self.source_fix is not None:
            _require_visible_id(self.source_fix, "RepairOption.source_fix")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": self.key,
            "kind": self.kind.value,
            "owner": self.owner.value,
            "auto_allowed": self.auto_allowed,
            "requires_evidence": self.requires_evidence,
            "requires_approval": self.requires_approval,
            "blocking": self.blocking,
        }
        if self.source_fix is not None:
            result["source_fix"] = self.source_fix
        return result


def _planner_option(key: str, *, source_fix: str | None = None) -> RepairOption:
    return RepairOption(
        key=key,
        kind=RepairOptionKind.PLAN_PATCH,
        owner=IssueOwner.PLANNER,
        auto_allowed=True,
        source_fix=source_fix,
    )


def _provider_option(key: str, *, source_fix: str | None = None) -> RepairOption:
    return RepairOption(
        key=key,
        kind=RepairOptionKind.PROVIDER_QUERY,
        owner=IssueOwner.PROVIDER,
        auto_allowed=True,
        requires_evidence=True,
        source_fix=source_fix,
    )


def _human_option(key: str, *, source_fix: str | None = None) -> RepairOption:
    return RepairOption(
        key=key,
        kind=RepairOptionKind.USER_DECISION,
        owner=IssueOwner.HUMAN,
        auto_allowed=False,
        requires_approval=True,
        source_fix=source_fix,
    )


def _system_option(
    key: str,
    *,
    auto_allowed: bool,
    blocking: bool = False,
    source_fix: str | None = None,
) -> RepairOption:
    return RepairOption(
        key=key,
        kind=RepairOptionKind.SYSTEM_ACTION,
        owner=IssueOwner.SYSTEM,
        auto_allowed=auto_allowed,
        blocking=blocking,
        source_fix=source_fix,
    )


_SYSTEM_FIXES = frozenset(
    {
        "persist_stable_activity_ids",
        "rebuild_day_travel_edges",
        "set_trip_date_range",
        "evaluate_with_as_of_time",
    }
)
_PROVIDER_FIXES = frozenset(
    {
        "set_trip_iana_timezone",
        "set_day_iana_timezone",
        "set_valid_iana_timezone",
        "fetch_travel_estimate",
        "refresh_travel_estimate",
        "refresh_travel_estimates",
        "verify_travel_estimate",
        "resolve_travel_evidence",
        "refresh_opening_hours",
        "verify_activity_fact",
        "verify_activity_durations",
        "verify_timing_evidence",
    }
)
_PLANNER_FIXES = frozenset(
    {
        "add_interday_transfer",
        "assign_activity_to_day",
        "change_inbound_route",
        "change_previous_activity",
        "change_return_route",
        "change_route",
        "choose_allowed_travel_mode",
        "move_activity",
        "move_activity_outside_dst_gap",
        "move_activity_to_allowed_day",
        "move_activity_to_another_day",
        "move_last_activity",
        "reorder_activities",
        "restore_fixed_time",
        "schedule_constraint_subjects",
        "schedule_dependencies",
        "schedule_interday_transfer",
        "schedule_required_activity",
        "set_activity_duration",
    }
)
_HUMAN_FIXES = frozenset(
    {
        "align_day_bases",
        "change_decision_state",
        "change_selected_choices",
        "extend_day_availability",
        "keep_one_variant",
        "reconcile_trip_and_day_dates",
        "remove_trigger_activity",
        "repair_constraint",
        "select_exactly_one_variant",
        "set_day_availability",
        "set_day_base_locations",
        "set_day_end_location",
        "set_day_start_location",
        "set_explicit_activity_datetime",
        "shorten_activity",
        "specify_dst_fold",
    }
)
_SPECIAL_FIXES = frozenset({"estimate_or_verify_activity_durations"})

FIX_REGISTRY_KEYS = (
    _SYSTEM_FIXES
    | _PROVIDER_FIXES
    | _PLANNER_FIXES
    | _HUMAN_FIXES
    | _SPECIAL_FIXES
)
"""Complete explicit registry of currently emitted suggested-fix keys."""


KNOWN_ISSUE_CODES = frozenset(
    {
        "SYNTHETIC_ACTIVITY_IDS",
        "INVALID_TRAVEL_REFERENCE",
        "DATE_RANGE_MISSING",
        "DAY_OUTSIDE_TRIP_RANGE",
        "TIMEZONE_FALLBACK",
        "DAY_TIMEZONE_INVALID",
        "INVALID_TIMEZONE",
        "MISSING_DAY_BOUNDS",
        "MISSING_START_LOCATION",
        "MISSING_END_LOCATION",
        "SCHEDULE_DATE_ROLLOVER_INFERRED",
        "ACTIVITY_DURATION_UNVERIFIED",
        "MISSING_DURATION",
        "MISSING_TRAVEL_ESTIMATE",
        "RECOMMENDED_MODE_UNAVAILABLE",
        "TRAVEL_EVIDENCE_UNVERIFIED",
        "UNVERIFIED_EVIDENCE",
        "STALE_EVIDENCE",
        "CONFLICTED_EVIDENCE",
        "INVALID_FRESHNESS_TIMESTAMP",
        "FRESHNESS_NOT_EVALUATED",
        "UNSCHEDULED_SELECTED_ACTIVITY",
        "DISALLOWED_MODE",
        "SCHEDULED_START_CONFLICT",
        "POSSIBLE_SCHEDULED_START_CONFLICT",
        "FIXED_TIME_CONFLICT",
        "POSSIBLE_FIXED_TIME_CONFLICT",
        "DAY_WINDOW_VIOLATION",
        "POSSIBLE_DAY_WINDOW_VIOLATION",
        "RETURN_AFTER_DAY_END",
        "POSSIBLE_RETURN_AFTER_DAY_END",
        "TIME_WINDOW_VIOLATION",
        "POSSIBLE_TIME_WINDOW_VIOLATION",
        "GLOBAL_TIMELINE_OVERLAP",
        "POSSIBLE_GLOBAL_TIMELINE_OVERLAP",
        "RETURN_TIMELINE_OVERLAP",
        "POSSIBLE_RETURN_TIMELINE_OVERLAP",
        "INBOUND_TIMELINE_OVERLAP",
        "POSSIBLE_INBOUND_TIMELINE_OVERLAP",
        "MISSING_REQUIRED_ACTIVITY",
        "EXACTLY_ONCE_VIOLATION",
        "AT_MOST_ONCE_VIOLATION",
        "PRECEDENCE_SUBJECT_MISSING",
        "PRECEDENCE_VIOLATION",
        "POSSIBLE_PRECEDENCE_VIOLATION",
        "REQUIRES_VIOLATION",
        "CHOOSE_N_VIOLATION",
        "ALLOWED_DAY_VIOLATION",
        "DAILY_LIMIT_EXCEEDED",
        "POSSIBLE_DAILY_LIMIT_EXCEEDED",
        "LOCATION_CONTINUITY_VIOLATION",
        "INVALID_CONSTRAINT",
        "CONSTRAINT_NEEDS_VERIFICATION",
        "NONEXISTENT_LOCAL_TIME",
        "AMBIGUOUS_LOCAL_TIME",
    }
)
"""Current kernel/load issue catalog. Unknown codes fail closed."""


def options_for_fix(fix: str) -> tuple[RepairOption, ...]:
    """Return explicit typed options; unknown fixes become manual review."""

    if fix in _SYSTEM_FIXES:
        return (_system_option(fix, auto_allowed=True),)
    if fix in _PROVIDER_FIXES:
        return (_provider_option(fix),)
    if fix in _PLANNER_FIXES:
        return (_planner_option(fix),)
    if fix in _HUMAN_FIXES:
        return (_human_option(fix),)
    if fix == "estimate_or_verify_activity_durations":
        return (
            _planner_option(
                "set_activity_duration",
                source_fix=fix,
            ),
            _provider_option(
                "verify_activity_durations",
                source_fix=fix,
            ),
        )
    return (
        _system_option(
            "manual_review",
            auto_allowed=False,
            blocking=True,
            source_fix=fix,
        ),
    )


def operation_matches_option(
    option_key: str,
    operation: PatchOperation,
) -> bool:
    """Return whether an operation is a conservative implementation of an option.

    A false result is not proof that the proposal is wrong.  It means the
    bounded controller must require a trusted checkpoint instead of applying
    the operation automatically.
    """

    if option_key == "set_activity_duration":
        return isinstance(operation, UpdateActivity) and set(
            operation.fields
        ) == {"duration_min"}
    if option_key in _PLACEMENT_OPTION_KEYS:
        return (
            isinstance(operation, PlaceActivity)
            and operation.scheduled_start is UNSET
        )
    if option_key in _EXPLICIT_TIME_OPTION_KEYS:
        return (
            isinstance(operation, PlaceActivity)
            and operation.scheduled_start is not UNSET
        ) or (
            isinstance(operation, UpdateActivity)
            and set(operation.fields) == {"time"}
        )
    if option_key == "add_interday_transfer":
        return isinstance(operation, AddActivity) and set(
            operation.fields
        ).issubset(_INTERDAY_ACTIVITY_FIELDS)
    if option_key == "schedule_interday_transfer":
        if isinstance(operation, PlaceActivity):
            return True
        return isinstance(operation, AddActivity) and set(
            operation.fields
        ).issubset(_INTERDAY_ACTIVITY_FIELDS)
    return False


def _manual_review_option(source: str | None = None) -> RepairOption:
    return _system_option(
        "manual_review",
        auto_allowed=False,
        blocking=True,
        source_fix=source,
    )


@dataclass(frozen=True, slots=True)
class RepairIssue:
    """One stable issue identity plus safe repair routing."""

    issue_id: str
    family: str
    check: CheckIssue
    owner: IssueOwner
    options: tuple[RepairOption, ...]

    def __post_init__(self) -> None:
        if not _ISSUE_ID_RE.fullmatch(self.issue_id):
            raise ValueError("RepairIssue.issue_id has an invalid form")
        _require_visible_id(self.family, "RepairIssue.family")
        if not isinstance(self.check, CheckIssue):
            raise TypeError("RepairIssue.check must be CheckIssue")
        if not isinstance(self.owner, IssueOwner):
            raise TypeError("RepairIssue.owner must be IssueOwner")
        if not isinstance(self.options, tuple) or not self.options:
            raise ValueError("RepairIssue.options must be a non-empty tuple")
        if any(not isinstance(option, RepairOption) for option in self.options):
            raise TypeError("RepairIssue.options contains an invalid value")

    @property
    def auto_repairable(self) -> bool:
        return any(option.auto_allowed for option in self.options) and not any(
            option.blocking for option in self.options
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "family": self.family,
            "code": self.check.code,
            "severity": self.check.severity.value,
            "message": self.check.message,
            "activity_ids": list(self.check.activity_ids),
            "evidence_refs": list(self.check.evidence_refs),
            "details": {
                key: value for key, value in self.check.details
            },
            "owner": self.owner.value,
            "options": [option.to_dict() for option in self.options],
        }


def issue_family(code: str) -> str:
    """Normalize possible/definite variants to one underlying family."""

    return code.removeprefix("POSSIBLE_")


def issue_scope_payload(issue: CheckIssue) -> dict[str, Any]:
    """Return the message/severity-independent structural identity payload."""

    return {
        "family": issue_family(issue.code),
        "activity_ids": sorted(issue.activity_ids),
        "evidence_refs": sorted(issue.evidence_refs),
        "details": [
            [key, value]
            for key, value in sorted(issue.details)
            if key in _ISSUE_SCOPE_DETAIL_KEYS
        ],
    }


def issue_id_for(issue: CheckIssue) -> str:
    digest = hashlib.sha256(
        b"trip-planner.repair-issue/v1\0"
        + canonical_json_bytes(issue_scope_payload(issue))
    ).hexdigest()
    return f"issue-{digest[:32]}"


def _issue_options(issue: CheckIssue) -> tuple[RepairOption, ...]:
    if issue.code not in KNOWN_ISSUE_CODES:
        return (_manual_review_option(issue.code),)
    result: list[RepairOption] = []
    seen: set[tuple[str, str, str]] = set()
    for fix in issue.suggested_fixes:
        for option in options_for_fix(fix):
            identity = (
                option.key,
                option.kind.value,
                option.source_fix or "",
            )
            if identity not in seen:
                result.append(option)
                seen.add(identity)
    if not result:
        result.append(_manual_review_option(issue.code))
    return tuple(result)


def _primary_owner(options: tuple[RepairOption, ...]) -> IssueOwner:
    if any(option.blocking for option in options):
        return IssueOwner.SYSTEM
    return options[0].owner


def build_repair_issues(report: CheckReport) -> tuple[RepairIssue, ...]:
    """Build deterministic issues, merging possible/definite family duplicates."""

    by_id: dict[str, RepairIssue] = {}
    for check in report.issues:
        issue_id = issue_id_for(check)
        options = _issue_options(check)
        candidate = RepairIssue(
            issue_id=issue_id,
            family=issue_family(check.code),
            check=check,
            owner=_primary_owner(options),
            options=options,
        )
        previous = by_id.get(issue_id)
        if previous is None:
            by_id[issue_id] = candidate
            continue
        previous_rank = _severity_rank(previous.check.severity)
        candidate_rank = _severity_rank(candidate.check.severity)
        selected = candidate if candidate_rank > previous_rank else previous
        merged_options: list[RepairOption] = []
        seen_options: set[tuple[str, str, str]] = set()
        for option in previous.options + candidate.options:
            key = (option.key, option.kind.value, option.source_fix or "")
            if key not in seen_options:
                merged_options.append(option)
                seen_options.add(key)
        by_id[issue_id] = RepairIssue(
            issue_id=issue_id,
            family=selected.family,
            check=selected.check,
            owner=_primary_owner(tuple(merged_options)),
            options=tuple(merged_options),
        )
    return tuple(by_id[key] for key in sorted(by_id))


def _severity_rank(value: IssueSeverity) -> int:
    return {
        IssueSeverity.INFO: 0,
        IssueSeverity.WARNING: 1,
        IssueSeverity.ERROR: 2,
    }[value]


@dataclass(frozen=True, slots=True)
class PlannerSnapshot:
    """Receipt-free immutable model input for one fixed evaluation instant."""

    contract_version: str
    snapshot_id: str
    trip_id: str
    revision: str
    evaluation_at: datetime
    state_digest: str
    state: FrozenJsonValue = field(repr=False)
    report: CheckReport = field(repr=False)
    issues: tuple[RepairIssue, ...] = field(repr=False)
    remaining_iterations: int
    remaining_provider_calls: int
    remaining_changes: int
    composed_state_digest: str = ""
    report_digest: str = ""
    decision_context_digest: str = ""
    evidence_binding: EvidenceBinding | None = None

    def __post_init__(self) -> None:
        if self.contract_version != SNAPSHOT_VERSION:
            raise ValueError("unsupported PlannerSnapshot contract version")
        _require_visible_id(self.snapshot_id, "PlannerSnapshot.snapshot_id")
        _require_visible_id(self.trip_id, "PlannerSnapshot.trip_id")
        _require_visible_id(self.revision, "PlannerSnapshot.revision")
        _require_aware_datetime(self.evaluation_at, "evaluation_at")
        _require_visible_id(self.state_digest, "PlannerSnapshot.state_digest")
        object.__setattr__(self, "state", freeze_json(self.state))
        if not isinstance(self.report, CheckReport):
            raise TypeError("PlannerSnapshot.report must be CheckReport")
        if not isinstance(self.issues, tuple) or any(
            not isinstance(issue, RepairIssue) for issue in self.issues
        ):
            raise TypeError("PlannerSnapshot.issues must contain RepairIssue values")
        for value, name in (
            (self.remaining_iterations, "remaining_iterations"),
            (self.remaining_provider_calls, "remaining_provider_calls"),
            (self.remaining_changes, "remaining_changes"),
        ):
            _require_non_negative_int(value, name)
        composed_digest = self.composed_state_digest or self.state_digest
        _require_visible_id(
            composed_digest,
            "PlannerSnapshot.composed_state_digest",
        )
        object.__setattr__(
            self,
            "composed_state_digest",
            composed_digest,
        )
        expected_report_digest = report_digest_for(self.report)
        if (
            self.report_digest
            and self.report_digest != expected_report_digest
        ):
            raise ValueError(
                "PlannerSnapshot.report_digest does not match report"
            )
        object.__setattr__(
            self,
            "report_digest",
            expected_report_digest,
        )
        if self.evidence_binding is not None:
            if type(self.evidence_binding) is not EvidenceBinding:
                raise TypeError(
                    "PlannerSnapshot.evidence_binding must be "
                    "EvidenceBinding or None"
                )
            if self.evidence_binding.evaluation_at != self.evaluation_at:
                raise ValueError(
                    "PlannerSnapshot evidence evaluation time must match"
                )
        expected_context_digest = decision_context_digest_for(
            evaluation_at=self.evaluation_at,
            state_digest=self.state_digest,
            composed_state_digest=composed_digest,
            report_digest=expected_report_digest,
            evidence_binding=self.evidence_binding,
        )
        if (
            self.decision_context_digest
            and self.decision_context_digest != expected_context_digest
        ):
            raise ValueError(
                "PlannerSnapshot.decision_context_digest does not match"
            )
        object.__setattr__(
            self,
            "decision_context_digest",
            expected_context_digest,
        )

    @property
    def issue_by_id(self) -> Mapping[str, RepairIssue]:
        return MappingProxyType(
            {issue.issue_id: issue for issue in self.issues}
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the complete model-facing snapshot without store internals."""

        evidence_bound = self.evidence_binding is not None
        return {
            "contract_version": self.contract_version,
            "snapshot_id": self.snapshot_id,
            "trip_id": self.trip_id,
            "revision": self.revision,
            "evaluation_at": self.evaluation_at.isoformat(),
            "state_digest": self.state_digest,
            "composed_state_digest": self.composed_state_digest,
            "report_digest": self.report_digest,
            "decision_context_digest": self.decision_context_digest,
            "evidence_binding": (
                self.evidence_binding.to_dict()
                if self.evidence_binding is not None
                else None
            ),
            "state": deep_copy_json(self.state),
            "report": (
                redacted_check_report_to_dict(
                    self.report,
                    report_digest=self.report_digest,
                )
                if evidence_bound
                else check_report_to_dict(self.report)
            ),
            "issues": [
                (
                    redacted_repair_issue_to_dict(issue)
                    if evidence_bound
                    else issue.to_dict()
                )
                for issue in self.issues
            ],
            "budget": {
                "remaining_iterations": self.remaining_iterations,
                "remaining_provider_calls": self.remaining_provider_calls,
                "remaining_changes": self.remaining_changes,
            },
        }


def semantic_state_digest(state: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        b"trip-planner.semantic-state/v1\0" + canonical_json_bytes(state)
    ).hexdigest()
    return f"state-{digest}"


def snapshot_from_plan(
    plan: Mapping[str, Any],
    *,
    evaluation_at: datetime,
    remaining_iterations: int,
    remaining_provider_calls: int,
    remaining_changes: int,
    evidence_snapshot: EvidenceSnapshot | None = None,
) -> PlannerSnapshot:
    """Build a deterministic snapshot from a strict canonical plan."""

    normalized_at = _normalized_utc(evaluation_at)
    if evidence_snapshot is not None:
        if type(evidence_snapshot) is not EvidenceSnapshot:
            raise TypeError(
                "evidence_snapshot must be EvidenceSnapshot or None"
            )
        if evidence_snapshot.evaluation_at != normalized_at:
            raise RepairContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "evidence snapshot evaluation time does not match planner",
            )
        composed = compose_trip_state(plan, evidence_snapshot)
        state_model = composed.state
        composed_state_digest = composed.composed_state_digest
        evidence_binding = composed.evidence_binding
    else:
        state_model = plan_to_trip_state(plan)
        composed_state_digest = trip_state_digest(state_model)
        evidence_binding = None
    report = evaluate_timeline(state_model, now=normalized_at)
    state_value = plan.get("state")
    if not isinstance(state_value, Mapping):
        raise RepairContractError(
            "MALFORMED_PLAN",
            "canonical plan state must be an object",
            path="$.state",
        )
    state_digest = semantic_state_digest(state_value)
    issues = build_repair_issues(report)
    report_digest = report_digest_for(report)
    revision = _required_mapping_text(plan, "revision")
    decision_context_digest = decision_context_digest_for(
        evaluation_at=normalized_at,
        state_digest=state_digest,
        composed_state_digest=composed_state_digest,
        report_digest=report_digest,
        evidence_binding=evidence_binding,
    )
    snapshot_payload = {
        "contract_version": SNAPSHOT_VERSION,
        "trip_id": plan.get("trip_id"),
        "revision": revision,
        "evaluation_at": normalized_at.isoformat(),
        "state_digest": state_digest,
        "composed_state_digest": composed_state_digest,
        "report_digest": report_digest,
        "decision_context_digest": decision_context_digest,
        "evidence_binding_digest": (
            evidence_binding.binding_digest
            if evidence_binding is not None
            else None
        ),
        "issue_ids": [issue.issue_id for issue in issues],
        "report_status": report.status.value,
        "remaining_iterations": remaining_iterations,
        "remaining_provider_calls": remaining_provider_calls,
        "remaining_changes": remaining_changes,
    }
    snapshot_hash = hashlib.sha256(
        b"trip-planner.snapshot/v1\0"
        + canonical_json_bytes(snapshot_payload)
    ).hexdigest()
    return PlannerSnapshot(
        contract_version=SNAPSHOT_VERSION,
        snapshot_id=f"snapshot-{snapshot_hash}",
        trip_id=_required_mapping_text(plan, "trip_id"),
        revision=revision,
        evaluation_at=normalized_at,
        state_digest=state_digest,
        state=state_value,
        report=report,
        issues=issues,
        remaining_iterations=remaining_iterations,
        remaining_provider_calls=remaining_provider_calls,
        remaining_changes=remaining_changes,
        composed_state_digest=composed_state_digest,
        report_digest=report_digest,
        decision_context_digest=decision_context_digest,
        evidence_binding=evidence_binding,
    )


def check_report_to_dict(report: CheckReport) -> dict[str, Any]:
    return {
        "status": report.status.value,
        "issues": [
            {
                "code": issue.code,
                "severity": issue.severity.value,
                "message": issue.message,
                "activity_ids": list(issue.activity_ids),
                "evidence_refs": list(issue.evidence_refs),
                "details": {key: value for key, value in issue.details},
                "suggested_fixes": list(issue.suggested_fixes),
            }
            for issue in report.issues
        ],
        "timeline": [
            {
                "activity_id": entry.activity_id,
                "day_id": entry.day_id,
                "location_id": entry.location_id,
                "arrival_at": entry.arrival_at.isoformat(),
                "start_at": entry.start_at.isoformat(),
                "end_at": entry.end_at.isoformat(),
                "travel_duration_min": entry.travel_duration_min,
                "wait_duration_min": entry.wait_duration_min,
                "slack_min": entry.slack_min,
            }
            for entry in report.timeline
        ],
        "day_summaries": [
            {
                "day_id": summary.day_id,
                "starts_at": (
                    summary.starts_at.isoformat()
                    if summary.starts_at is not None
                    else None
                ),
                "completes_at": (
                    summary.completes_at.isoformat()
                    if summary.completes_at is not None
                    else None
                ),
                "available_end_at": (
                    summary.available_end_at.isoformat()
                    if summary.available_end_at is not None
                    else None
                ),
                "end_slack_min": summary.end_slack_min,
                "activity_count": summary.activity_count,
                "service_min": summary.service_min,
                "travel_min": summary.travel_min,
                "buffer_min": summary.buffer_min,
                "wait_min": summary.wait_min,
                "timing_verified": summary.timing_verified,
            }
            for summary in report.day_summaries
        ],
        "metrics": {key: value for key, value in report.metrics},
    }


def report_digest_for(report: CheckReport) -> str:
    if not isinstance(report, CheckReport):
        raise TypeError("report must be CheckReport")
    digest = hashlib.sha256(
        b"trip-planner.check-report/v1\0"
        + canonical_json_bytes(check_report_to_dict(report))
    ).hexdigest()
    return f"report-{digest}"


def decision_context_digest_for(
    *,
    evaluation_at: datetime,
    state_digest: str,
    composed_state_digest: str,
    report_digest: str,
    evidence_binding: EvidenceBinding | None,
) -> str:
    normalized_at = _normalized_utc(evaluation_at)
    for value, name in (
        (state_digest, "state_digest"),
        (composed_state_digest, "composed_state_digest"),
        (report_digest, "report_digest"),
    ):
        _require_visible_id(value, name)
    if evidence_binding is not None and type(evidence_binding) is not EvidenceBinding:
        raise TypeError("evidence_binding must be EvidenceBinding or None")
    digest = hashlib.sha256(
        b"trip-planner.decision-context/v1\0"
        + canonical_json_bytes(
            {
                "evaluation_at": normalized_at.isoformat(),
                "state_digest": state_digest,
                "composed_state_digest": composed_state_digest,
                "report_digest": report_digest,
                "evidence_binding_digest": (
                    evidence_binding.binding_digest
                    if evidence_binding is not None
                    else None
                ),
            }
        )
    ).hexdigest()
    return f"decision-{digest}"


def redacted_check_report_to_dict(
    report: CheckReport,
    *,
    report_digest: str,
) -> dict[str, Any]:
    """Return evidence-safe report identity without derived provider values."""

    return {
        "status": report.status.value,
        "report_digest": report_digest,
        "issue_count": len(report.issues),
        "timeline_entry_count": len(report.timeline),
        "day_summary_count": len(report.day_summaries),
    }


def redacted_repair_issue_to_dict(issue: RepairIssue) -> dict[str, Any]:
    """Return repair routing without provider-derived message or detail values."""

    return {
        "issue_id": issue.issue_id,
        "family": issue.family,
        "code": issue.check.code,
        "severity": issue.check.severity.value,
        "activity_ids": list(issue.check.activity_ids),
        "evidence_refs": list(issue.check.evidence_refs),
        "owner": issue.owner.value,
        "options": [option.to_dict() for option in issue.options],
    }


@dataclass(frozen=True, slots=True)
class RepairBudget:
    max_iterations: int = 8
    max_provider_calls: int = 12
    max_changes: int = 40
    max_auto_changes_per_patch: int = 12

    def __post_init__(self) -> None:
        for value, name in (
            (self.max_iterations, "max_iterations"),
            (self.max_provider_calls, "max_provider_calls"),
            (self.max_changes, "max_changes"),
            (self.max_auto_changes_per_patch, "max_auto_changes_per_patch"),
        ):
            _require_non_negative_int(value, name)
        if self.max_auto_changes_per_patch > self.max_changes:
            raise ValueError(
                "max_auto_changes_per_patch cannot exceed max_changes"
            )


@dataclass(frozen=True, slots=True)
class OperationReason:
    op_id: str
    issue_ids: tuple[str, ...]
    option_key: str
    reason: str

    def __post_init__(self) -> None:
        _require_visible_id(self.op_id, "OperationReason.op_id")
        if not isinstance(self.issue_ids, tuple) or not self.issue_ids:
            raise ValueError("OperationReason.issue_ids must be a non-empty tuple")
        if len(set(self.issue_ids)) != len(self.issue_ids):
            raise ValueError("OperationReason.issue_ids must be unique")
        for issue_id in self.issue_ids:
            if not _ISSUE_ID_RE.fullmatch(issue_id):
                raise ValueError(
                    f"OperationReason contains invalid issue ID {issue_id!r}"
                )
        _require_visible_id(self.option_key, "OperationReason.option_key")
        _require_bounded_text(
            self.reason,
            "OperationReason.reason",
            maximum=_MAX_REASON_LENGTH,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "issue_ids": list(self.issue_ids),
            "option_key": self.option_key,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ProposalIntent:
    """The complete model-authored boundary: operations plus explanations."""

    operations: tuple[PatchOperation, ...]
    reasons: tuple[OperationReason, ...]
    summary: str = ""
    proposal_version: str = PROPOSAL_VERSION

    def __post_init__(self) -> None:
        if self.proposal_version != PROPOSAL_VERSION:
            raise RepairContractError(
                "UNSUPPORTED_PROPOSAL_VERSION",
                f"expected {PROPOSAL_VERSION!r}",
                path="$.proposal_version",
            )
        if not isinstance(self.operations, tuple):
            object.__setattr__(self, "operations", tuple(self.operations))
        if not self.operations:
            raise RepairContractError(
                "EMPTY_PROPOSAL",
                "proposal must contain at least one operation",
                path="$.operations",
            )
        if len(self.operations) > _MAX_PROPOSAL_OPERATIONS:
            raise RepairContractError(
                "PROPOSAL_TOO_COMPLEX",
                "proposal may contain at most "
                f"{_MAX_PROPOSAL_OPERATIONS} operations",
                path="$.operations",
            )
        if any(
            not isinstance(
                operation,
                (
                    AddActivity,
                    UpdateActivity,
                    PlaceActivity,
                    RemoveActivity,
                    UpdateDay,
                    AddConstraint,
                    UpdateConstraint,
                    RemoveConstraint,
                ),
            )
            for operation in self.operations
        ):
            raise RepairContractError(
                "MALFORMED_PROPOSAL",
                "operations contains an unsupported value",
                path="$.operations",
            )
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))
        if any(not isinstance(reason, OperationReason) for reason in self.reasons):
            raise RepairContractError(
                "MALFORMED_PROPOSAL",
                "reasons contains an unsupported value",
                path="$.reasons",
            )
        _require_bounded_optional_text(
            self.summary,
            "ProposalIntent.summary",
            maximum=_MAX_SUMMARY_LENGTH,
        )
        operation_ids = tuple(operation.op_id for operation in self.operations)
        reason_ids = tuple(reason.op_id for reason in self.reasons)
        if len(set(operation_ids)) != len(operation_ids):
            raise RepairContractError(
                "DUPLICATE_OPERATION_ID",
                "proposal operation IDs must be unique",
                path="$.operations",
            )
        if len(set(reason_ids)) != len(reason_ids):
            raise RepairContractError(
                "DUPLICATE_OPERATION_REASON",
                "each operation may have exactly one reason",
                path="$.reasons",
            )
        if set(operation_ids) != set(reason_ids):
            raise RepairContractError(
                "OPERATION_REASON_MISMATCH",
                "operation and reason IDs must match exactly",
                path="$",
            )

    @property
    def reason_by_op_id(self) -> Mapping[str, OperationReason]:
        return MappingProxyType({reason.op_id: reason for reason in self.reasons})

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_version": self.proposal_version,
            "summary": self.summary,
            "operations": operations_to_dict(self.operations),
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


@dataclass(frozen=True, slots=True)
class BoundProposal:
    """Trusted storage binding for one model-authored proposal."""

    snapshot_id: str
    patch: PlanPatch
    reasons: tuple[OperationReason, ...]
    effect_digest: str
    attempt_digest: str


def operations_to_dict(
    operations: tuple[PatchOperation, ...],
) -> list[dict[str, Any]]:
    placeholder = PlanPatch(
        trip_id="repair-binding",
        base_revision="0" * 64,
        idempotency_key="repair-binding",
        operations=operations,
    )
    return patch_to_dict(placeholder)["operations"]


def effect_digest(operations: tuple[PatchOperation, ...]) -> str:
    """Hash semantic effects while excluding model-controlled operation IDs."""

    normalized: list[dict[str, Any]] = []
    for operation in operations_to_dict(operations):
        value = deep_copy_json(operation)
        assert isinstance(value, dict)
        value.pop("op_id", None)
        normalized.append(value)
    digest = hashlib.sha256(
        b"trip-planner.repair-effect/v1\0"
        + canonical_json_bytes(
            {
                "patch_version": "plan-patch/v1",
                "operations": normalized,
            }
        )
    ).hexdigest()
    return f"effect-{digest}"


def attempt_digest(decision_context_digest: str, effect: str) -> str:
    digest = hashlib.sha256(
        b"trip-planner.repair-attempt/v2\0"
        + canonical_json_bytes(
            {
                "decision_context_digest": decision_context_digest,
                "effect_digest": effect,
            }
        )
    ).hexdigest()
    return f"attempt-{digest}"


def bind_proposal(
    snapshot: PlannerSnapshot,
    proposal: ProposalIntent,
    *,
    run_id: str,
) -> BoundProposal:
    """Inject trusted trip/revision/idempotency and validate cited issues."""

    if not isinstance(snapshot, PlannerSnapshot):
        raise TypeError("snapshot must be PlannerSnapshot")
    if not isinstance(proposal, ProposalIntent):
        raise TypeError("proposal must be ProposalIntent")
    _require_visible_id(run_id, "run_id")
    known_issue_ids = set(snapshot.issue_by_id)
    for reason in proposal.reasons:
        unknown = sorted(set(reason.issue_ids) - known_issue_ids)
        if unknown:
            raise RepairContractError(
                "UNKNOWN_ISSUE_ID",
                f"operation {reason.op_id!r} cites unknown issues: {unknown}",
                path="$.reasons",
            )
        for issue_id in reason.issue_ids:
            issue = snapshot.issue_by_id[issue_id]
            if not issue.auto_repairable or not any(
                option.key == reason.option_key
                and option.kind is RepairOptionKind.PLAN_PATCH
                and option.auto_allowed
                and not option.blocking
                for option in issue.options
            ):
                raise RepairContractError(
                    "INVALID_REPAIR_OPTION",
                    (
                        f"operation {reason.op_id!r} cannot use option "
                        f"{reason.option_key!r} for issue {issue_id!r}"
                    ),
                    path="$.reasons",
                )
    bound_operations, bound_reasons = _bind_model_operations(proposal)
    effect = effect_digest(bound_operations)
    attempt = attempt_digest(snapshot.decision_context_digest, effect)
    key_hash = hashlib.sha256(
        canonical_json_bytes(
            {
                "run_id": run_id,
                "snapshot_id": snapshot.snapshot_id,
                "intent": proposal.summary,
                "operations": operations_to_dict(bound_operations),
            }
        )
    ).hexdigest()
    patch = PlanPatch(
        trip_id=snapshot.trip_id,
        base_revision=snapshot.revision,
        idempotency_key=f"repair-{key_hash}",
        operations=bound_operations,
        intent=proposal.summary,
    )
    return BoundProposal(
        snapshot_id=snapshot.snapshot_id,
        patch=patch,
        reasons=bound_reasons,
        effect_digest=effect,
        attempt_digest=attempt,
    )


def _bind_model_operations(
    proposal: ProposalIntent,
) -> tuple[tuple[PatchOperation, ...], tuple[OperationReason, ...]]:
    """Stamp trusted audit IDs and AI provenance onto model-authored effects."""

    reason_by_id = proposal.reason_by_op_id
    operations: list[PatchOperation] = []
    reasons: list[OperationReason] = []
    for index, operation in enumerate(proposal.operations, start=1):
        trusted_op_id = f"ai-op-{index:03d}"
        if isinstance(operation, AddActivity):
            bound: PatchOperation = AddActivity(
                op_id=trusted_op_id,
                activity_id=operation.activity_id,
                day_id=operation.day_id,
                fields=operation.fields,
                position=operation.position,
                anchor_activity_id=operation.anchor_activity_id,
            )
        elif isinstance(operation, UpdateActivity):
            bound = UpdateActivity(
                trusted_op_id,
                operation.activity_id,
                operation.fields,
            )
        elif isinstance(operation, PlaceActivity):
            bound = PlaceActivity(
                op_id=trusted_op_id,
                activity_id=operation.activity_id,
                day_id=operation.day_id,
                position=operation.position,
                anchor_activity_id=operation.anchor_activity_id,
                scheduled_start=operation.scheduled_start,
            )
        elif isinstance(operation, RemoveActivity):
            bound = RemoveActivity(trusted_op_id, operation.activity_id)
        elif isinstance(operation, UpdateDay):
            bound = UpdateDay(trusted_op_id, operation.day_id, operation.fields)
        elif isinstance(operation, AddConstraint):
            fields = deep_copy_json(operation.fields)
            assert isinstance(fields, dict)
            fields["origin"] = "ai"
            bound = AddConstraint(
                trusted_op_id,
                operation.constraint_id,
                fields,
            )
        elif isinstance(operation, UpdateConstraint):
            if "origin" in operation.fields:
                raise RepairContractError(
                    "FORBIDDEN_AUTHORITY_WRITE",
                    "AI proposals cannot change constraint origin",
                    path="$.operations",
                )
            bound = UpdateConstraint(
                trusted_op_id,
                operation.constraint_id,
                operation.fields,
            )
        elif isinstance(operation, RemoveConstraint):
            bound = RemoveConstraint(trusted_op_id, operation.constraint_id)
        else:  # pragma: no cover - ProposalIntent rejects this earlier
            raise TypeError(f"unsupported operation {type(operation).__name__}")
        operations.append(bound)
        source_reason = reason_by_id[operation.op_id]
        reasons.append(
            OperationReason(
                op_id=trusted_op_id,
                issue_ids=source_reason.issue_ids,
                option_key=source_reason.option_key,
                reason=source_reason.reason,
            )
        )
    return tuple(operations), tuple(reasons)


def decode_proposal_intent(
    data: bytes | bytearray | memoryview | str,
) -> ProposalIntent:
    """Strictly decode a model response, rejecting duplicate/unknown fields."""

    if isinstance(data, str):
        encoded_size = len(data.encode("utf-8"))
    elif isinstance(data, (bytes, bytearray, memoryview)):
        encoded_size = len(data)
    else:
        raise TypeError("proposal input must be bytes-like or str")
    if encoded_size > _MAX_PROPOSAL_BYTES:
        raise RepairContractError(
            "PROPOSAL_TOO_LARGE",
            f"proposal must be at most {_MAX_PROPOSAL_BYTES} bytes",
        )
    try:
        value = decode_json_bytes(data)
    except PlanCodecError as exc:
        raise RepairContractError(
            exc.code,
            exc.message,
            path=exc.path,
        ) from exc
    try:
        return _proposal_intent_from_value(value)
    except RepairContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            str(exc),
            path="$",
        ) from exc


def _proposal_intent_from_value(value: Any) -> ProposalIntent:
    root = _strict_mapping(
        value,
        "$",
        required={"proposal_version", "operations", "reasons"},
        optional={"summary"},
    )
    version = _required_text(root.get("proposal_version"), "$.proposal_version")
    operations_value = _strict_sequence(root.get("operations"), "$.operations")
    if len(operations_value) > _MAX_PROPOSAL_OPERATIONS:
        raise RepairContractError(
            "PROPOSAL_TOO_COMPLEX",
            "proposal may contain at most "
            f"{_MAX_PROPOSAL_OPERATIONS} operations",
            path="$.operations",
        )
    reasons_value = _strict_sequence(root.get("reasons"), "$.reasons")
    operations = tuple(
        _parse_operation(item, f"$.operations[{index}]")
        for index, item in enumerate(operations_value)
    )
    reasons = tuple(
        _parse_reason(item, f"$.reasons[{index}]")
        for index, item in enumerate(reasons_value)
    )
    summary = root.get("summary", "")
    if not isinstance(summary, str):
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            "summary must be a string",
            path="$.summary",
        )
    return ProposalIntent(
        proposal_version=version,
        operations=operations,
        reasons=reasons,
        summary=summary,
    )


def _parse_operation(value: Any, path: str) -> PatchOperation:
    if not isinstance(value, Mapping):
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            "expected an object",
            path=path,
        )
    raw = deep_copy_json(value)
    assert isinstance(raw, dict)
    missing_header = {"op", "op_id"} - set(raw)
    if missing_header:
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            f"missing fields: {', '.join(sorted(missing_header))}",
            path=path,
        )
    op = _required_text(raw.get("op"), f"{path}.op")
    op_id = _required_text(raw.get("op_id"), f"{path}.op_id")
    if op == "add_activity":
        raw = _strict_mapping(
            value,
            path,
            required={"op", "op_id", "activity_id", "day_id", "fields"},
            optional={"position", "anchor_activity_id"},
        )
        return AddActivity(
            op_id=op_id,
            activity_id=_required_text(
                raw.get("activity_id"), f"{path}.activity_id"
            ),
            day_id=_required_text(raw.get("day_id"), f"{path}.day_id"),
            fields=_strict_mapping(
                raw.get("fields"),
                f"{path}.fields",
                optional=ACTIVITY_MUTABLE_FIELDS,
            ),
            position=_parse_placement(raw.get("position", "end"), path),
            anchor_activity_id=_optional_string(
                raw.get("anchor_activity_id"),
                f"{path}.anchor_activity_id",
            ),
        )
    if op == "update_activity":
        raw = _strict_mapping(
            value,
            path,
            required={"op", "op_id", "activity_id", "fields"},
        )
        return UpdateActivity(
            op_id,
            _required_text(raw.get("activity_id"), f"{path}.activity_id"),
            _strict_mapping(
                raw.get("fields"),
                f"{path}.fields",
                optional=ACTIVITY_MUTABLE_FIELDS,
            ),
        )
    if op == "place_activity":
        raw = _strict_mapping(
            value,
            path,
            required={"op", "op_id", "activity_id", "day_id"},
            optional={
                "position",
                "anchor_activity_id",
                "scheduled_start",
            },
        )
        scheduled_start: str | None | Any = UNSET
        if "scheduled_start" in raw:
            scheduled_start = raw["scheduled_start"]
            if scheduled_start is not None and not isinstance(
                scheduled_start, str
            ):
                raise RepairContractError(
                    "MALFORMED_PROPOSAL",
                    "scheduled_start must be a string or null",
                    path=f"{path}.scheduled_start",
                )
        return PlaceActivity(
            op_id=op_id,
            activity_id=_required_text(
                raw.get("activity_id"), f"{path}.activity_id"
            ),
            day_id=_required_text(raw.get("day_id"), f"{path}.day_id"),
            position=_parse_placement(raw.get("position", "end"), path),
            anchor_activity_id=_optional_string(
                raw.get("anchor_activity_id"),
                f"{path}.anchor_activity_id",
            ),
            scheduled_start=scheduled_start,
        )
    if op == "remove_activity":
        raw = _strict_mapping(
            value,
            path,
            required={"op", "op_id", "activity_id"},
        )
        return RemoveActivity(
            op_id,
            _required_text(raw.get("activity_id"), f"{path}.activity_id"),
        )
    if op == "update_day":
        raw = _strict_mapping(
            value,
            path,
            required={"op", "op_id", "day_id", "fields"},
        )
        return UpdateDay(
            op_id,
            _required_text(raw.get("day_id"), f"{path}.day_id"),
            _strict_mapping(
                raw.get("fields"),
                f"{path}.fields",
                optional=DAY_MUTABLE_FIELDS,
            ),
        )
    if op == "add_constraint":
        raw = _strict_mapping(
            value,
            path,
            required={"op", "op_id", "constraint_id", "fields"},
        )
        return AddConstraint(
            op_id,
            _required_text(
                raw.get("constraint_id"), f"{path}.constraint_id"
            ),
            _strict_mapping(
                raw.get("fields"),
                f"{path}.fields",
                optional=_AI_CONSTRAINT_MUTABLE_FIELDS,
            ),
        )
    if op == "update_constraint":
        raw = _strict_mapping(
            value,
            path,
            required={"op", "op_id", "constraint_id", "fields"},
        )
        return UpdateConstraint(
            op_id,
            _required_text(
                raw.get("constraint_id"), f"{path}.constraint_id"
            ),
            _strict_mapping(
                raw.get("fields"),
                f"{path}.fields",
                optional=_AI_CONSTRAINT_MUTABLE_FIELDS,
            ),
        )
    if op == "remove_constraint":
        raw = _strict_mapping(
            value,
            path,
            required={"op", "op_id", "constraint_id"},
        )
        return RemoveConstraint(
            op_id,
            _required_text(
                raw.get("constraint_id"), f"{path}.constraint_id"
            ),
        )
    raise RepairContractError(
        "UNKNOWN_OPERATION",
        f"unsupported proposal operation {op!r}",
        path=f"{path}.op",
    )


def _parse_reason(value: Any, path: str) -> OperationReason:
    raw = _strict_mapping(
        value,
        path,
        required={"op_id", "issue_ids", "option_key", "reason"},
    )
    issue_values = _strict_sequence(
        raw.get("issue_ids"), f"{path}.issue_ids"
    )
    return OperationReason(
        op_id=_required_text(raw.get("op_id"), f"{path}.op_id"),
        issue_ids=tuple(
            _required_text(item, f"{path}.issue_ids[{index}]")
            for index, item in enumerate(issue_values)
        ),
        option_key=_required_text(
            raw.get("option_key"),
            f"{path}.option_key",
        ),
        reason=_required_bounded_text(
            raw.get("reason"),
            f"{path}.reason",
            maximum=_MAX_REASON_LENGTH,
        ),
    )


def _parse_placement(value: Any, path: str) -> Placement:
    if not isinstance(value, str):
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            "position must be a string",
            path=f"{path}.position",
        )
    try:
        return Placement(value)
    except ValueError as exc:
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            f"unknown position {value!r}",
            path=f"{path}.position",
        ) from exc


def _strict_mapping(
    value: Any,
    path: str,
    *,
    required: set[str] | frozenset[str] = frozenset(),
    optional: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            "expected an object",
            path=path,
        )
    result = deep_copy_json(value)
    assert isinstance(result, dict)
    actual = set(result)
    missing = required - actual
    unknown = actual - required - optional
    if missing:
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            f"missing fields: {', '.join(sorted(missing))}",
            path=path,
        )
    if unknown:
        raise RepairContractError(
            "FORBIDDEN_PROPOSAL_FIELD",
            f"unknown or trusted-only fields: {', '.join(sorted(unknown))}",
            path=path,
        )
    return result


def _strict_sequence(value: Any, path: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            "expected an array",
            path=path,
        )
    return [deep_copy_json(item) for item in value]


def _required_text(value: Any, path: str) -> str:
    try:
        _require_visible_id(value, path)
    except (TypeError, ValueError) as exc:
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            str(exc),
            path=path,
        ) from exc
    assert isinstance(value, str)
    return value


def _required_bounded_text(value: Any, path: str, *, maximum: int) -> str:
    try:
        _require_bounded_text(value, path, maximum=maximum)
    except (TypeError, ValueError) as exc:
        raise RepairContractError(
            "MALFORMED_PROPOSAL",
            str(exc),
            path=path,
        ) from exc
    assert isinstance(value, str)
    return value


def _optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path)


def _required_mapping_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RepairContractError(
            "MALFORMED_PLAN",
            f"canonical plan requires {key}",
            path=f"$.{key}",
        )
    return item


def _require_visible_id(value: Any, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ID_LENGTH
        or value != value.strip()
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise ValueError(
            f"{name} must be 1-{_MAX_ID_LENGTH} visible characters without "
            "leading/trailing whitespace or control characters"
        )


def _require_bounded_text(value: Any, name: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-empty text up to {maximum} characters")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{name} cannot contain control characters")


def _require_bounded_optional_text(
    value: Any, name: str, *, maximum: int
) -> None:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name} must be text up to {maximum} characters")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{name} cannot contain control characters")


def _require_aware_datetime(value: Any, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _normalized_utc(value: datetime) -> datetime:
    _require_aware_datetime(value, "evaluation_at")
    return value.astimezone(timezone.utc)


def _require_non_negative_int(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


__all__ = [
    "FIX_REGISTRY_KEYS",
    "KNOWN_ISSUE_CODES",
    "PROPOSAL_VERSION",
    "SNAPSHOT_VERSION",
    "BoundProposal",
    "IssueOwner",
    "OperationReason",
    "PlannerSnapshot",
    "ProposalIntent",
    "RepairBudget",
    "RepairContractError",
    "RepairIssue",
    "RepairOption",
    "RepairOptionKind",
    "attempt_digest",
    "bind_proposal",
    "build_repair_issues",
    "check_report_to_dict",
    "decode_proposal_intent",
    "decision_context_digest_for",
    "effect_digest",
    "issue_family",
    "issue_id_for",
    "issue_scope_payload",
    "operation_matches_option",
    "operations_to_dict",
    "options_for_fix",
    "report_digest_for",
    "semantic_state_digest",
    "snapshot_from_plan",
]
