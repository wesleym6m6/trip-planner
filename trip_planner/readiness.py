"""Pure, fail-closed Phase 4.6A trip readiness projection.

This module does not read or write a store, contact providers, migrate data,
render output, or create mutation authority.  It recomputes the deterministic
kernel check and exposes only bounded readiness codes, counts, and digests.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .codec import plan_to_trip_state, validate_plan
from .composition import ComposedTripState, compose_trip_state
from .facts import EvidenceSnapshot, FactKey, FactKind, FactObservation
from .lodging import (
    LodgingIntakeAssessment,
    LodgingIntakeStatus,
    LodgingRequirement,
)
from .lodging_confirmation import (
    LodgingConfirmationReview,
    LodgingConfirmationState,
)
from .models import (
    CheckStatus,
    EvidenceState,
    IssueSeverity,
    TravelEstimate,
)
from .repair import report_digest_for
from .scheduling import trip_state_digest
from .timeline import evaluate_composed_timeline


READINESS_VERSION = "trip-readiness/v1"
_MAX_COUNT = 4096
_PROBLEM_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_STATE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REPORT_DIGEST_RE = re.compile(r"report-[0-9a-f]{64}")
_SUMMARY_TOKEN = object()
_READINESS_TOKEN = object()
_SUMMARY_ZH = {
    "draft": "行程仍在草擬；先補齊必要資訊。",
    "review": "行程可供審閱，但仍有確認或驗證事項。",
}
_TRAVEL_READY_RECHECK_SUMMARY_ZH = (
    "目前資料足以出發使用；請在指定時間前重新確認。"
)
_TRAVEL_READY_NO_DEADLINE_SUMMARY_ZH = (
    "目前資料足以出發使用；目前沒有指定的重新確認期限。"
)


class ReadinessStatus(str, Enum):
    """One product profile; it is deliberately not a kernel status."""

    DRAFT = "draft"
    REVIEW = "review"
    TRAVEL_READY = "travel_ready"


class ReadinessSource(str, Enum):
    """Closed source classes safe to expose without source payloads."""

    CANONICAL = "canonical"
    KERNEL = "kernel"
    EVIDENCE = "evidence"
    LODGING = "lodging"
    REVIEW = "review"


class ReadinessAction(str, Enum):
    """The only next actions Phase 4.6A may recommend."""

    FIX_INFEASIBLE_PLAN = "fix_infeasible_plan"
    RECOMPOSE_TRIP_STATE = "recompose_trip_state"
    RESOLVE_CONFLICT = "resolve_conflict"
    COMPLETE_LODGING = "complete_lodging"
    RESTAGE_LODGING_REVIEW = "restage_lodging_review"
    REFRESH_EVIDENCE = "refresh_evidence"
    CONFIRM_LODGING = "confirm_lodging"
    NONE = "none"


_ACTION_RANK = {
    ReadinessAction.FIX_INFEASIBLE_PLAN: 0,
    ReadinessAction.RECOMPOSE_TRIP_STATE: 1,
    ReadinessAction.RESOLVE_CONFLICT: 2,
    ReadinessAction.COMPLETE_LODGING: 3,
    ReadinessAction.RESTAGE_LODGING_REVIEW: 4,
    ReadinessAction.REFRESH_EVIDENCE: 5,
    ReadinessAction.CONFIRM_LODGING: 6,
    ReadinessAction.NONE: 7,
}
_SEVERITY_RANK = {
    IssueSeverity.ERROR: 0,
    IssueSeverity.WARNING: 1,
    IssueSeverity.INFO: 2,
}


def _summary_zh(
    status: ReadinessStatus,
    recheck_required_at: datetime | None,
) -> str:
    if status is not ReadinessStatus.TRAVEL_READY:
        return _SUMMARY_ZH[status.value]
    if recheck_required_at is not None:
        return _TRAVEL_READY_RECHECK_SUMMARY_ZH
    return _TRAVEL_READY_NO_DEADLINE_SUMMARY_ZH


def _utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _identity(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise ValueError(f"{name} must be bounded visible text")
    return value


def _digest_value(
    value: object,
    name: str,
    *,
    state_digest: bool = False,
) -> str:
    pattern = _STATE_DIGEST_RE if state_digest else _DIGEST_RE
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _count(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_COUNT:
        raise ValueError(f"{name} must be a bounded non-negative integer")
    return value


def _report_digest_value(value: object, name: str) -> str:
    if type(value) is not str or _REPORT_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be an exact report digest")
    return value


def _canonical_digest(value: Any, *, prefix: str) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(
        prefix.encode("utf-8") + b"\n" + encoded
    ).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalLodgingSummary:
    """Validated, private-data-free lodging projection from one plan."""

    trip_id: str
    plan_revision: str
    canonical_state_digest: str
    stay_count: int
    night_count: int
    decision_states: tuple[str, ...]
    evidence_states: tuple[str, ...]
    covered_nights: tuple[date, ...] = field(repr=False)
    binding_digest: str = ""
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _SUMMARY_TOKEN:
            raise ValueError(
                "CanonicalLodgingSummary must come from a validated plan"
            )
        trip_id = _identity(self.trip_id, "trip_id")
        revision = _digest_value(self.plan_revision, "plan_revision")
        state_digest = _digest_value(
            self.canonical_state_digest,
            "canonical_state_digest",
            state_digest=True,
        )
        stay_count = _count(self.stay_count, "stay_count")
        night_count = _count(self.night_count, "night_count")
        decisions = tuple(sorted(set(self.decision_states)))
        evidence = tuple(sorted(set(self.evidence_states)))
        if any(
            item not in {"selected", "fixed", "booked"}
            for item in decisions
        ):
            raise ValueError("decision_states contain an invalid value")
        if any(item != "unverified" for item in evidence):
            raise ValueError("canonical lodging evidence must stay unverified")
        if (
            not isinstance(self.covered_nights, tuple)
            or any(
                not isinstance(item, date)
                or isinstance(item, datetime)
                for item in self.covered_nights
            )
        ):
            raise TypeError("covered_nights must contain exact dates")
        covered = tuple(sorted(set(self.covered_nights)))
        if len(covered) != night_count:
            raise ValueError("night_count differs from covered nights")
        if bool(stay_count) != bool(covered):
            raise ValueError("stay and covered-night counts disagree")
        payload = {
            "trip_id": trip_id,
            "plan_revision": revision,
            "canonical_state_digest": state_digest,
            "stay_count": stay_count,
            "night_count": night_count,
            "decision_states": list(decisions),
            "evidence_states": list(evidence),
            "covered_nights": [item.isoformat() for item in covered],
        }
        expected = _canonical_digest(
            payload,
            prefix="canonical-lodging-summary",
        )
        if self.binding_digest and self.binding_digest != expected:
            raise ValueError(
                "CanonicalLodgingSummary.binding_digest differs from content"
            )
        object.__setattr__(self, "trip_id", trip_id)
        object.__setattr__(self, "plan_revision", revision)
        object.__setattr__(
            self,
            "canonical_state_digest",
            state_digest,
        )
        object.__setattr__(self, "stay_count", stay_count)
        object.__setattr__(self, "night_count", night_count)
        object.__setattr__(self, "decision_states", decisions)
        object.__setattr__(self, "evidence_states", evidence)
        object.__setattr__(self, "covered_nights", covered)
        object.__setattr__(self, "binding_digest", expected)

    @classmethod
    def from_plan(
        cls,
        plan: Mapping[str, Any],
        *,
        composed: ComposedTripState,
    ) -> "CanonicalLodgingSummary":
        """Validate and bind lodging to one exact composed canonical view."""

        if not isinstance(plan, Mapping):
            raise TypeError("plan must be a mapping")
        if type(composed) is not ComposedTripState:
            raise TypeError("composed must be exact ComposedTripState")
        validate_plan(plan)
        if (
            plan["trip_id"] != composed.trip_id
            or plan["revision"] != composed.plan_revision
        ):
            raise ValueError("plan identity differs from composed state")
        canonical_state_digest = trip_state_digest(
            plan_to_trip_state(plan)
        )
        if canonical_state_digest != composed.canonical_state_digest:
            raise ValueError(
                "plan content differs from composed canonical state"
            )
        state_value = plan["state"]
        assert isinstance(state_value, Mapping)
        trip_value = state_value["trip"]
        assert isinstance(trip_value, Mapping)
        lodging_values = trip_value.get("lodgings", [])
        assert isinstance(lodging_values, list)

        covered: set[date] = set()
        decisions: set[str] = set()
        evidence: set[str] = set()
        for item in lodging_values:
            assert isinstance(item, Mapping)
            check_in = date.fromisoformat(str(item["check_in"]))
            check_out = date.fromisoformat(str(item["check_out"]))
            cursor = check_in
            while cursor < check_out:
                covered.add(cursor)
                cursor = date.fromordinal(cursor.toordinal() + 1)
            decisions.add(str(item["decision_state"]))
            evidence.add(str(item["evidence_state"]))

        return cls(
            trip_id=str(plan["trip_id"]),
            plan_revision=str(plan["revision"]),
            canonical_state_digest=canonical_state_digest,
            stay_count=len(lodging_values),
            night_count=len(covered),
            decision_states=tuple(decisions),
            evidence_states=tuple(evidence),
            covered_nights=tuple(covered),
            _token=_SUMMARY_TOKEN,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return counts and states only; dates and property IDs stay private."""

        return {
            "stay_count": self.stay_count,
            "night_count": self.night_count,
            "decision_states": list(self.decision_states),
            "evidence_states": list(self.evidence_states),
            "binding_digest": self.binding_digest,
        }

    def __repr__(self) -> str:
        return (
            "CanonicalLodgingSummary("
            f"binding_digest={self.binding_digest!r}, "
            f"stay_count={self.stay_count!r}, "
            f"night_count={self.night_count!r})"
        )


@dataclass(frozen=True, slots=True)
class ReadinessProblem:
    """One stable problem without provider, lodging, or activity payloads."""

    code: str
    severity: IssueSeverity
    source: ReadinessSource
    next_action: ReadinessAction
    affected_count: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.code) is not str
            or _PROBLEM_CODE_RE.fullmatch(self.code) is None
        ):
            raise ValueError("readiness problem code is invalid")
        if type(self.severity) is not IssueSeverity:
            raise TypeError("severity must be exact IssueSeverity")
        if type(self.source) is not ReadinessSource:
            raise TypeError("source must be exact ReadinessSource")
        if type(self.next_action) is not ReadinessAction:
            raise TypeError("next_action must be exact ReadinessAction")
        affected = _count(self.affected_count, "affected_count")
        if affected == 0:
            raise ValueError("affected_count must be positive")
        object.__setattr__(self, "affected_count", affected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "source": self.source.value,
            "next_action": self.next_action.value,
            "affected_count": self.affected_count,
        }


@dataclass(frozen=True, slots=True)
class TripReadiness:
    """Factory-only safe readiness result bound to exact runtime identity."""

    trip_ref: str
    plan_revision: str
    canonical_state_digest: str
    composed_state_digest: str
    kernel_report_digest: str
    canonical_lodging_digest: str
    evidence_binding_digest: str
    evidence_snapshot_id: str
    evaluated_at: datetime
    status: ReadinessStatus
    recheck_required_at: datetime | None
    used_evidence_count: int
    lodging_stay_count: int
    lodging_night_count: int
    next_action: ReadinessAction
    problems: tuple[ReadinessProblem, ...]
    readiness_id: str = ""
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _READINESS_TOKEN:
            raise ValueError("TripReadiness must come from the assessor")
        trip_ref = _digest_value(self.trip_ref, "trip_ref")
        revision = _digest_value(self.plan_revision, "plan_revision")
        state_digest = _digest_value(
            self.canonical_state_digest,
            "canonical_state_digest",
            state_digest=True,
        )
        composed_digest = _digest_value(
            self.composed_state_digest,
            "composed_state_digest",
            state_digest=True,
        )
        report_digest = _report_digest_value(
            self.kernel_report_digest,
            "kernel_report_digest",
        )
        lodging_digest = _digest_value(
            self.canonical_lodging_digest,
            "canonical_lodging_digest",
        )
        binding_digest = _digest_value(
            self.evidence_binding_digest,
            "evidence_binding_digest",
        )
        snapshot_id = _digest_value(
            self.evidence_snapshot_id,
            "evidence_snapshot_id",
        )
        evaluated = _utc(self.evaluated_at, "evaluated_at")
        if type(self.status) is not ReadinessStatus:
            raise TypeError("status must be exact ReadinessStatus")
        recheck = (
            _utc(self.recheck_required_at, "recheck_required_at")
            if self.recheck_required_at is not None
            else None
        )
        if recheck is not None and recheck < evaluated:
            raise ValueError("recheck_required_at cannot be in the past")
        used_count = _count(
            self.used_evidence_count,
            "used_evidence_count",
        )
        stay_count = _count(self.lodging_stay_count, "lodging_stay_count")
        night_count = _count(
            self.lodging_night_count,
            "lodging_night_count",
        )
        if type(self.next_action) is not ReadinessAction:
            raise TypeError("next_action must be exact ReadinessAction")
        if (
            not isinstance(self.problems, tuple)
            or any(
                type(item) is not ReadinessProblem
                for item in self.problems
            )
        ):
            raise TypeError("problems must contain exact readiness problems")
        expected_problems = _normalized_problems(self.problems)
        if expected_problems != self.problems:
            raise ValueError("problems must be normalized deterministically")
        expected_status = _status_for(expected_problems)
        if self.status is not expected_status:
            raise ValueError("status differs from readiness problems")
        expected_action = (
            expected_problems[0].next_action
            if expected_problems
            else ReadinessAction.NONE
        )
        if self.next_action is not expected_action:
            raise ValueError("next_action differs from readiness problems")
        payload = {
            "contract_version": READINESS_VERSION,
            "trip_ref": trip_ref,
            "plan_revision": revision,
            "canonical_state_digest": state_digest,
            "composed_state_digest": composed_digest,
            "kernel_report_digest": report_digest,
            "canonical_lodging_digest": lodging_digest,
            "evidence_binding_digest": binding_digest,
            "evidence_snapshot_id": snapshot_id,
            "evaluated_at": evaluated.isoformat(),
            "status": self.status.value,
            "recheck_required_at": (
                recheck.isoformat() if recheck is not None else None
            ),
            "used_evidence_count": used_count,
            "lodging_stay_count": stay_count,
            "lodging_night_count": night_count,
            "next_action": self.next_action.value,
            "problems": [
                item.to_dict() for item in expected_problems
            ],
        }
        expected_id = _canonical_digest(
            payload,
            prefix="trip-readiness",
        )
        if self.readiness_id and self.readiness_id != expected_id:
            raise ValueError("readiness_id differs from content")
        object.__setattr__(self, "trip_ref", trip_ref)
        object.__setattr__(self, "plan_revision", revision)
        object.__setattr__(
            self,
            "canonical_state_digest",
            state_digest,
        )
        object.__setattr__(
            self,
            "composed_state_digest",
            composed_digest,
        )
        object.__setattr__(
            self,
            "kernel_report_digest",
            report_digest,
        )
        object.__setattr__(
            self,
            "canonical_lodging_digest",
            lodging_digest,
        )
        object.__setattr__(
            self,
            "evidence_binding_digest",
            binding_digest,
        )
        object.__setattr__(self, "evidence_snapshot_id", snapshot_id)
        object.__setattr__(self, "evaluated_at", evaluated)
        object.__setattr__(self, "recheck_required_at", recheck)
        object.__setattr__(self, "used_evidence_count", used_count)
        object.__setattr__(self, "lodging_stay_count", stay_count)
        object.__setattr__(self, "lodging_night_count", night_count)
        object.__setattr__(self, "problems", expected_problems)
        object.__setattr__(self, "readiness_id", expected_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": READINESS_VERSION,
            "trip_ref": self.trip_ref,
            "plan_revision": self.plan_revision,
            "canonical_state_digest": self.canonical_state_digest,
            "composed_state_digest": self.composed_state_digest,
            "kernel_report_digest": self.kernel_report_digest,
            "canonical_lodging_digest": self.canonical_lodging_digest,
            "evidence_binding_digest": self.evidence_binding_digest,
            "evidence_snapshot_id": self.evidence_snapshot_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "status": self.status.value,
            "summary_zh": _summary_zh(
                self.status,
                self.recheck_required_at,
            ),
            "recheck_required_at": (
                self.recheck_required_at.isoformat()
                if self.recheck_required_at is not None
                else None
            ),
            "used_evidence_count": self.used_evidence_count,
            "lodging_stay_count": self.lodging_stay_count,
            "lodging_night_count": self.lodging_night_count,
            "next_action": self.next_action.value,
            "problems": [item.to_dict() for item in self.problems],
            "readiness_id": self.readiness_id,
        }


def assess_trip_readiness(
    *,
    canonical_plan: Mapping[str, Any],
    composed: ComposedTripState,
    snapshot: EvidenceSnapshot,
    availability_keys: tuple[FactKey, ...] = (),
    lodging_intake: LodgingIntakeAssessment | None = None,
    pending_lodging_review: LodgingConfirmationReview | None = None,
) -> TripReadiness:
    """Recompose and assess one exact runtime view without external effects."""

    if not isinstance(canonical_plan, Mapping):
        raise TypeError("canonical_plan must be a mapping")
    if type(composed) is not ComposedTripState:
        raise TypeError("composed must be exact ComposedTripState")
    if type(snapshot) is not EvidenceSnapshot:
        raise TypeError("snapshot must be exact EvidenceSnapshot")
    if (
        not isinstance(availability_keys, tuple)
        or any(type(item) is not FactKey for item in availability_keys)
    ):
        raise TypeError("availability_keys must contain exact FactKey values")
    if (
        lodging_intake is not None
        and type(lodging_intake) is not LodgingIntakeAssessment
    ):
        raise TypeError("lodging_intake must be exact when provided")
    if (
        pending_lodging_review is not None
        and type(pending_lodging_review)
        is not LodgingConfirmationReview
    ):
        raise TypeError(
            "pending_lodging_review must be exact when provided"
        )

    expected_composed = compose_trip_state(
        canonical_plan,
        snapshot,
        availability_keys=availability_keys,
    )
    canonical_lodging = CanonicalLodgingSummary.from_plan(
        canonical_plan,
        composed=expected_composed,
    )
    now = _utc(snapshot.evaluation_at, "snapshot.evaluation_at")
    raw_problems: list[ReadinessProblem] = []
    canonical_matches = (
        composed.trip_id == expected_composed.trip_id
        and composed.plan_revision == expected_composed.plan_revision
        and composed.canonical_state_digest
        == expected_composed.canonical_state_digest
    )
    if not canonical_matches:
        _add_problem(
            raw_problems,
            "CANONICAL_BINDING_MISMATCH",
            IssueSeverity.ERROR,
            ReadinessSource.CANONICAL,
            ReadinessAction.RECOMPOSE_TRIP_STATE,
        )

    binding = composed.evidence
    binding_matches = (
        binding.policy_registry_revision == snapshot.policies.revision
        and binding.store_revision == snapshot.store_revision
        and binding.evidence_revision == snapshot.evidence_revision
        and binding.outcome_revision == snapshot.outcome_revision
        and binding.evaluation_at == now
        and binding.purge_checked_at
        == _utc(snapshot.purge_checked_at, "snapshot.purge_checked_at")
        and binding.snapshot_id == snapshot.snapshot_id
    )
    if not binding_matches:
        _add_problem(
            raw_problems,
            "EVIDENCE_BINDING_MISMATCH",
            IssueSeverity.ERROR,
            ReadinessSource.EVIDENCE,
            ReadinessAction.REFRESH_EVIDENCE,
        )

    passed_attribution_ready = _check_live_attribution(
        composed,
        raw_problems,
    )
    if composed != expected_composed:
        _add_problem(
            raw_problems,
            "COMPOSITION_BINDING_MISMATCH",
            IssueSeverity.ERROR,
            ReadinessSource.EVIDENCE,
            ReadinessAction.RECOMPOSE_TRIP_STATE,
        )
    expected_attribution_ready = _check_live_attribution(
        expected_composed,
        raw_problems,
    )
    evidence_ready, deadlines = _check_used_evidence(
        expected_composed,
        snapshot,
        now,
        raw_problems,
        binding_matches=True,
        attribution_ready=(
            passed_attribution_ready
            and expected_attribution_ready
            and binding_matches
            and composed == expected_composed
        ),
    )
    _check_unresolved_routes(
        expected_composed,
        snapshot,
        raw_problems,
    )

    report = evaluate_composed_timeline(expected_composed, now=now)
    if report.status is CheckStatus.INFEASIBLE:
        _add_problem(
            raw_problems,
            "PLAN_INFEASIBLE",
            IssueSeverity.ERROR,
            ReadinessSource.KERNEL,
            ReadinessAction.FIX_INFEASIBLE_PLAN,
            affected_count=max(1, len(report.errors)),
        )
    elif report.status is CheckStatus.NEEDS_VERIFICATION:
        _add_problem(
            raw_problems,
            "PLAN_NEEDS_VERIFICATION",
            IssueSeverity.WARNING,
            ReadinessSource.KERNEL,
            ReadinessAction.REFRESH_EVIDENCE,
            affected_count=max(1, len(report.warnings)),
        )

    _check_lodging(
        expected_composed,
        canonical_lodging,
        lodging_intake,
        pending_lodging_review,
        raw_problems,
    )
    review_deadline = _check_pending_review(
        expected_composed,
        pending_lodging_review,
        lodging_intake,
        now,
        raw_problems,
    )

    problems = _normalized_problems(tuple(raw_problems))
    suppress_evidence_deadline = any(
        item.next_action
        in {
            ReadinessAction.RECOMPOSE_TRIP_STATE,
            ReadinessAction.REFRESH_EVIDENCE,
            ReadinessAction.RESOLVE_CONFLICT,
        }
        for item in problems
    )
    recheck_candidates = (
        []
        if suppress_evidence_deadline or not evidence_ready
        else list(deadlines)
    )
    if review_deadline is not None:
        recheck_candidates.append(review_deadline)
    recheck = min(recheck_candidates) if recheck_candidates else None
    status = _status_for(problems)
    next_action = (
        problems[0].next_action if problems else ReadinessAction.NONE
    )
    return TripReadiness(
        trip_ref=_canonical_digest(
            expected_composed.trip_id,
            prefix="trip-readiness-trip-ref",
        ),
        plan_revision=expected_composed.plan_revision,
        canonical_state_digest=expected_composed.canonical_state_digest,
        composed_state_digest=expected_composed.composed_state_digest,
        kernel_report_digest=report_digest_for(report),
        canonical_lodging_digest=canonical_lodging.binding_digest,
        evidence_binding_digest=expected_composed.evidence.binding_digest,
        evidence_snapshot_id=snapshot.snapshot_id,
        evaluated_at=now,
        status=status,
        recheck_required_at=recheck,
        used_evidence_count=len(
            expected_composed.evidence.used_observation_ids
        ),
        lodging_stay_count=canonical_lodging.stay_count,
        lodging_night_count=canonical_lodging.night_count,
        next_action=next_action,
        problems=problems,
        _token=_READINESS_TOKEN,
    )


def _check_live_attribution(
    composed: ComposedTripState,
    problems: list[ReadinessProblem],
) -> bool:
    binding = composed.evidence
    if not binding.requires_live_attribution:
        return True
    labels = {item.label for item in composed.live_attributions}
    observation_ids = {
        item.observation_id for item in composed.live_attributions
    }
    missing_labels = set(
        binding.required_attribution_labels
    ).difference(labels)
    missing_observations = set(
        binding.used_observation_ids
    ).difference(observation_ids)
    if not missing_labels and not missing_observations:
        return True
    _add_problem(
        problems,
        "LIVE_ATTRIBUTION_MISSING",
        IssueSeverity.ERROR,
        ReadinessSource.EVIDENCE,
        ReadinessAction.REFRESH_EVIDENCE,
        affected_count=max(
            1,
            len(missing_labels) + len(missing_observations),
        ),
    )
    return False


def _check_used_evidence(
    composed: ComposedTripState,
    snapshot: EvidenceSnapshot,
    now: datetime,
    problems: list[ReadinessProblem],
    *,
    binding_matches: bool,
    attribution_ready: bool,
) -> tuple[bool, tuple[datetime, ...]]:
    observations = {
        item.observation_id: item for item in snapshot.observations
    }
    ready = binding_matches and attribution_ready
    deadlines: list[datetime] = []
    for observation_id in composed.evidence.used_observation_ids:
        observation = observations.get(observation_id)
        if observation is None:
            ready = False
            _add_problem(
                problems,
                "USED_EVIDENCE_MISSING",
                IssueSeverity.ERROR,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )
            continue
        if not observation.retained_at(now):
            ready = False
            _add_problem(
                problems,
                "EVIDENCE_RETENTION_EXPIRED",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )
            continue
        resolution = snapshot.resolve(observation.key)
        if resolution.evidence_state is EvidenceState.CONFLICTED:
            ready = False
            _add_problem(
                problems,
                "EVIDENCE_CONFLICTED",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.RESOLVE_CONFLICT,
            )
            continue
        if (
            resolution.evidence_state is EvidenceState.STALE
            or not observation.fresh_at(now)
        ):
            ready = False
            _add_problem(
                problems,
                "RECHECK_REQUIRED",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )
            continue
        if resolution.evidence_state is EvidenceState.UNVERIFIED:
            ready = False
            _add_problem(
                problems,
                "EVIDENCE_MISSING",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )
            continue
        if (
            resolution.selected is None
            or resolution.selected.observation_id != observation_id
        ):
            ready = False
            _add_problem(
                problems,
                "EVIDENCE_SELECTION_DRIFT",
                IssueSeverity.ERROR,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )
            continue
        if not resolution.supports_travel_ready_use:
            ready = False
            _add_problem(
                problems,
                "EVIDENCE_NOT_TRAVEL_READY",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )
            continue
        deadline = _utc(
            observation.valid_until,
            "observation.valid_until",
        )
        if observation.purge_at is not None:
            deadline = min(
                deadline,
                _utc(observation.purge_at, "observation.purge_at"),
            )
        deadlines.append(deadline)
    return ready, tuple(deadlines)


def _check_unresolved_routes(
    composed: ComposedTripState,
    snapshot: EvidenceSnapshot,
    problems: list[ReadinessProblem],
) -> None:
    """Classify route gaps that composition correctly left canonical."""

    keys_by_scope: dict[tuple[object, ...], dict[str, FactKey]] = {}
    observations_by_key: dict[str, list[FactObservation]] = {}
    for observation in snapshot.observations:
        if observation.key.kind is not FactKind.ROUTE_ESTIMATE:
            continue
        scope = _route_key_scope(observation.key)
        if scope is None:
            continue
        keys_by_scope.setdefault(scope, {})[
            observation.key.key_id
        ] = observation.key
        observations_by_key.setdefault(
            observation.key.key_id,
            [],
        ).append(observation)

    for estimate in composed.state.travel_estimates:
        if estimate.evidence_state is not EvidenceState.UNVERIFIED:
            continue
        keys = keys_by_scope.get(_route_estimate_scope(estimate), {})
        if not keys:
            _add_problem(
                problems,
                "EVIDENCE_MISSING",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )
            continue
        observations = [
            observation
            for key_id in keys
            for observation in observations_by_key.get(key_id, ())
        ]
        if observations and not any(
            observation.retained_at(snapshot.evaluation_at)
            for observation in observations
        ):
            _add_problem(
                problems,
                "EVIDENCE_RETENTION_EXPIRED",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )
            continue
        states = {
            snapshot.resolve(key).evidence_state
            for key in keys.values()
        }
        if EvidenceState.CONFLICTED in states:
            _add_problem(
                problems,
                "EVIDENCE_CONFLICTED",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.RESOLVE_CONFLICT,
            )
        elif EvidenceState.STALE in states:
            _add_problem(
                problems,
                "RECHECK_REQUIRED",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )
        elif EvidenceState.UNVERIFIED in states:
            _add_problem(
                problems,
                "EVIDENCE_MISSING",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )
        else:
            _add_problem(
                problems,
                "EVIDENCE_NOT_TRAVEL_READY",
                IssueSeverity.WARNING,
                ReadinessSource.EVIDENCE,
                ReadinessAction.REFRESH_EVIDENCE,
            )


def _route_estimate_scope(
    estimate: TravelEstimate,
) -> tuple[object, ...]:
    return (
        estimate.from_location_id,
        estimate.to_location_id,
        estimate.mode,
        _optional_utc_iso(estimate.query_departure_at),
        _optional_utc_iso(estimate.query_arrival_at),
    )


def _route_key_scope(key: FactKey) -> tuple[object, ...] | None:
    subject_ids = key.subject_ids
    qualifiers = key.qualifier_map
    if len(subject_ids) != 2 or type(qualifiers.get("mode")) is not str:
        return None
    supported = {"mode", "departure_at", "arrival_at"}
    if set(qualifiers).difference(supported):
        return None
    return (
        subject_ids[0],
        subject_ids[1],
        qualifiers["mode"],
        _optional_datetime_text(qualifiers.get("departure_at")),
        _optional_datetime_text(qualifiers.get("arrival_at")),
    )


def _optional_utc_iso(value: object) -> str | None:
    if value is None:
        return None
    return _utc(value, "route query timestamp").isoformat()


def _optional_datetime_text(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    try:
        return _utc(parsed, "route qualifier timestamp").isoformat()
    except ValueError:
        return ""


def _check_lodging(
    composed: ComposedTripState,
    summary: CanonicalLodgingSummary,
    intake: LodgingIntakeAssessment | None,
    pending_review: LodgingConfirmationReview | None,
    problems: list[ReadinessProblem],
) -> None:
    if summary.stay_count:
        _add_problem(
            problems,
            "LODGING_EVIDENCE_UNVERIFIED",
            IssueSeverity.WARNING,
            ReadinessSource.LODGING,
            ReadinessAction.REFRESH_EVIDENCE,
            affected_count=summary.stay_count,
        )
    if intake is None:
        has_waiting_review = (
            pending_review is not None
            and pending_review.state
            is LodgingConfirmationState.WAITING_CONFIRMATION
        )
        if not summary.stay_count and not has_waiting_review:
            _add_problem(
                problems,
                "LODGING_REQUIREMENT_UNKNOWN",
                IssueSeverity.WARNING,
                ReadinessSource.LODGING,
                ReadinessAction.COMPLETE_LODGING,
            )
        return

    expected_start = composed.state.start_date
    expected_end = composed.state.end_date + timedelta(days=1)
    if (
        intake.stay_start != expected_start
        or intake.stay_end != expected_end
    ):
        _add_problem(
            problems,
            "LODGING_INTAKE_MISMATCH",
            IssueSeverity.ERROR,
            ReadinessSource.LODGING,
            ReadinessAction.RESOLVE_CONFLICT,
        )
        return
    day_dates = {item.date for item in composed.state.days}
    assessment_dates = set(
        intake.required_nights
        + intake.option_missing_nights
        + intake.undecided_nights
        + intake.conflicting_nights
    )
    if not assessment_dates.issubset(day_dates):
        _add_problem(
            problems,
            "LODGING_INTAKE_MISMATCH",
            IssueSeverity.ERROR,
            ReadinessSource.LODGING,
            ReadinessAction.RESOLVE_CONFLICT,
        )
        return
    if intake.requirement is LodgingRequirement.NOT_REQUIRED:
        if summary.stay_count:
            _add_problem(
                problems,
                "LODGING_REQUIREMENT_CONFLICT",
                IssueSeverity.ERROR,
                ReadinessSource.LODGING,
                ReadinessAction.RESOLVE_CONFLICT,
            )
        return
    if summary.stay_count:
        if (
            intake.requirement is LodgingRequirement.REQUIRED
            and not set(intake.required_nights).issubset(
                summary.covered_nights
            )
        ):
            _add_problem(
                problems,
                "LODGING_INCOMPLETE",
                IssueSeverity.ERROR,
                ReadinessSource.LODGING,
                ReadinessAction.COMPLETE_LODGING,
            )
        return

    if intake.status is LodgingIntakeStatus.CONFLICTED:
        _add_problem(
            problems,
            "LODGING_CONFLICT",
            IssueSeverity.ERROR,
            ReadinessSource.LODGING,
            ReadinessAction.RESOLVE_CONFLICT,
        )
    elif intake.status is LodgingIntakeStatus.PARTIAL:
        _add_problem(
            problems,
            "LODGING_INCOMPLETE",
            IssueSeverity.ERROR,
            ReadinessSource.LODGING,
            ReadinessAction.COMPLETE_LODGING,
        )
    elif intake.status is LodgingIntakeStatus.MISSING:
        _add_problem(
            problems,
            "LODGING_INCOMPLETE",
            (
                IssueSeverity.ERROR
                if intake.requirement is LodgingRequirement.REQUIRED
                else IssueSeverity.WARNING
            ),
            ReadinessSource.LODGING,
            ReadinessAction.COMPLETE_LODGING,
        )
    elif intake.status is LodgingIntakeStatus.SEEKING_OPTIONS:
        _add_problem(
            problems,
            "LODGING_OPTIONS_REQUIRED",
            IssueSeverity.WARNING,
            ReadinessSource.LODGING,
            ReadinessAction.COMPLETE_LODGING,
        )
    elif intake.status in {
        LodgingIntakeStatus.COMPARING,
        LodgingIntakeStatus.AWAITING_CONFIRMATION,
    }:
        _add_problem(
            problems,
            "LODGING_CONFIRMATION_REQUIRED",
            IssueSeverity.WARNING,
            ReadinessSource.LODGING,
            ReadinessAction.CONFIRM_LODGING,
        )


def _check_pending_review(
    composed: ComposedTripState,
    review: LodgingConfirmationReview | None,
    intake: LodgingIntakeAssessment | None,
    now: datetime,
    problems: list[ReadinessProblem],
) -> datetime | None:
    if review is None:
        return None
    if (
        review.trip_id != composed.trip_id
        or review.base_revision != composed.plan_revision
        or now < review.created_at
    ):
        _add_problem(
            problems,
            "LODGING_REVIEW_MISMATCH",
            IssueSeverity.ERROR,
            ReadinessSource.REVIEW,
            ReadinessAction.RESTAGE_LODGING_REVIEW,
        )
        return None
    if review.state is LodgingConfirmationState.WAITING_CONFIRMATION:
        if (
            intake is not None
            and intake.requirement is LodgingRequirement.NOT_REQUIRED
        ):
            _add_problem(
                problems,
                "LODGING_REVIEW_REQUIREMENT_CONFLICT",
                IssueSeverity.ERROR,
                ReadinessSource.REVIEW,
                ReadinessAction.RESOLVE_CONFLICT,
            )
            return None
        expired = now > review.expires_at
        _add_problem(
            problems,
            (
                "LODGING_REVIEW_EXPIRED"
                if expired
                else "LODGING_CONFIRMATION_PENDING"
            ),
            IssueSeverity.WARNING,
            ReadinessSource.REVIEW,
            (
                ReadinessAction.RESTAGE_LODGING_REVIEW
                if expired
                else ReadinessAction.CONFIRM_LODGING
            ),
        )
        return None if expired else review.expires_at
    elif review.state is LodgingConfirmationState.REJECTED:
        _add_problem(
            problems,
            "LODGING_REVIEW_REJECTED",
            IssueSeverity.ERROR,
            ReadinessSource.REVIEW,
            ReadinessAction.RESTAGE_LODGING_REVIEW,
        )
    return None


def _add_problem(
    problems: list[ReadinessProblem],
    code: str,
    severity: IssueSeverity,
    source: ReadinessSource,
    action: ReadinessAction,
    *,
    affected_count: int = 1,
) -> None:
    problems.append(
        ReadinessProblem(
            code=code,
            severity=severity,
            source=source,
            next_action=action,
            affected_count=affected_count,
        )
    )


def _normalized_problems(
    problems: tuple[ReadinessProblem, ...],
) -> tuple[ReadinessProblem, ...]:
    grouped: dict[
        tuple[str, IssueSeverity, ReadinessSource, ReadinessAction],
        int,
    ] = {}
    for item in problems:
        if type(item) is not ReadinessProblem:
            raise TypeError("problems must contain exact values")
        key = (
            item.code,
            item.severity,
            item.source,
            item.next_action,
        )
        grouped[key] = min(
            _MAX_COUNT,
            grouped.get(key, 0) + item.affected_count,
        )
    normalized = tuple(
        ReadinessProblem(
            code=key[0],
            severity=key[1],
            source=key[2],
            next_action=key[3],
            affected_count=count,
        )
        for key, count in grouped.items()
    )
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                _SEVERITY_RANK[item.severity],
                _ACTION_RANK[item.next_action],
                item.source.value,
                item.code,
            ),
        )
    )


def _status_for(
    problems: tuple[ReadinessProblem, ...],
) -> ReadinessStatus:
    if any(
        item.severity is IssueSeverity.ERROR for item in problems
    ):
        return ReadinessStatus.DRAFT
    if problems:
        return ReadinessStatus.REVIEW
    return ReadinessStatus.TRAVEL_READY


__all__ = [
    "READINESS_VERSION",
    "CanonicalLodgingSummary",
    "ReadinessAction",
    "ReadinessProblem",
    "ReadinessSource",
    "ReadinessStatus",
    "TripReadiness",
    "assess_trip_readiness",
]
