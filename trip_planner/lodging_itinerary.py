"""Evidence-aware joint lodging and itinerary recommendations.

Phase 4.5C remains a pure runtime sidecar.  It compares detached what-if
schedule problems whose lodging anchors differ, but it never promotes a
lodging candidate, creates a PlanPatch, writes canonical state, or claims that
the first result is a user decision.

Ranking is intentionally lexicographic rather than weighted.  Numeric route
features are admitted only when the 4.5B receipt, the schedule projection, and
one exact EvidenceSnapshot all agree.  Missing, stale, conflicted, mixed-basis,
or unverified values make the whole comparison unrankable instead of receiving
an invented zero or fallback value.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Any

from .availability import AvailabilityDisposition
from .composition import (
    ComposedTripState,
    EvidenceBinding,
    project_activity_availability,
)
from .facts import (
    EvidenceSnapshot,
    FactContractError,
    FactKind,
    FactObservation,
)
from .lodging_evidence import (
    LodgingComparisonAssessment,
    LodgingComparisonCandidate,
    LodgingEvidenceBasis,
    LodgingRouteDisposition,
    LodgingRouteEvidence,
)
from .models import (
    EvidenceState,
    IssueSeverity,
    TravelEstimate,
    TripState,
)
from .scheduling import (
    ReplanScope,
    ScheduleCandidate,
    ScheduleContractError,
    SchedulePreferences,
    ScheduleProblem,
    ScheduleResult,
    ScheduleScore,
    ScheduleStatus,
    SearchLimits,
    replay_schedule_candidate,
    schedule_problem_from_composed,
    trip_state_digest,
)


LODGING_ITINERARY_VERSION = "lodging-itinerary/v1"
_MAX_OPTIONS = 64
_MAX_OPTION_CANDIDATES = 16
_MAX_DAY_ANCHORS = 366
_MAX_ROUTE_USES = 512
_MAX_ISSUES = 2_048
_MAX_BUFFER_MIN = 24 * 60
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MACHINE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")


class LodgingItineraryStatus(str, Enum):
    """Overall recommendation state without decision authority."""

    RANKED = "ranked"
    NOT_RANKED = "not_ranked"
    INFEASIBLE = "infeasible"
    NEEDS_VERIFICATION = "needs_verification"
    NO_LODGING_OPTIONS = "no_lodging_options"


class LodgingOptionDisposition(str, Enum):
    """Whether one what-if option may participate in numeric comparison."""

    COMPARABLE = "comparable"
    INFEASIBLE = "infeasible"
    NEEDS_VERIFICATION = "needs_verification"


def _canonical_digest(value: Any, *, prefix: str) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(
        prefix.encode("utf-8") + b"\n" + encoded
    ).hexdigest()


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _schedule_digest(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or _DIGEST_RE.fullmatch(value[7:]) is None
    ):
        raise ValueError(f"{name} must be a prefixed SHA-256 digest")
    return value


def _machine_id(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if _MACHINE_ID_RE.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a bounded machine identifier")
    return normalized


def _exact_date(value: object, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be an exact date")
    return value


def _bounded_non_negative_int(
    value: object,
    name: str,
    *,
    maximum: int,
) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum}")
    return value


def _deci(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float, Decimal))
    ):
        raise TypeError(f"{name} must be an exact finite number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    result = int(
        (number * Decimal(10)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
    if result > 10**12:
        raise ValueError(f"{name} exceeds the bounded scoring range")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class LodgingDayAnchor:
    """Candidate-owned start/end lodging role for one detached planning day."""

    day_id: str
    start_candidate_id: str | None = None
    end_candidate_id: str | None = None
    anchor_id: str = ""

    def __post_init__(self) -> None:
        day_id = _machine_id(self.day_id, "LodgingDayAnchor.day_id")
        start = (
            _digest(
                self.start_candidate_id,
                "LodgingDayAnchor.start_candidate_id",
            )
            if self.start_candidate_id is not None
            else None
        )
        end = (
            _digest(
                self.end_candidate_id,
                "LodgingDayAnchor.end_candidate_id",
            )
            if self.end_candidate_id is not None
            else None
        )
        if start is None and end is None:
            raise ValueError(
                "LodgingDayAnchor requires a start or end lodging candidate"
            )
        expected = _canonical_digest(
            {
                "day_id": day_id,
                "start_candidate_id": start,
                "end_candidate_id": end,
            },
            prefix="lodging-day-anchor",
        )
        if self.anchor_id and self.anchor_id != expected:
            raise ValueError(
                "LodgingDayAnchor.anchor_id does not match content"
            )
        object.__setattr__(self, "day_id", day_id)
        object.__setattr__(self, "start_candidate_id", start)
        object.__setattr__(self, "end_candidate_id", end)
        object.__setattr__(self, "anchor_id", expected)

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    value
                    for value in (
                        self.start_candidate_id,
                        self.end_candidate_id,
                    )
                    if value is not None
                }
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "day_id": self.day_id,
            "start_candidate_id": self.start_candidate_id,
            "end_candidate_id": self.end_candidate_id,
        }


@dataclass(frozen=True, slots=True)
class LodgingRouteUse:
    """One semantic comparison slot backed by a 4.5B route receipt."""

    slot_id: str
    day_id: str
    candidate_id: str
    probe_id: str
    minimum_buffer_min: int = 0
    use_id: str = ""

    def __post_init__(self) -> None:
        slot_id = _machine_id(self.slot_id, "LodgingRouteUse.slot_id")
        day_id = _machine_id(self.day_id, "LodgingRouteUse.day_id")
        candidate_id = _digest(
            self.candidate_id,
            "LodgingRouteUse.candidate_id",
        )
        probe_id = _digest(
            self.probe_id,
            "LodgingRouteUse.probe_id",
        )
        minimum_buffer = _bounded_non_negative_int(
            self.minimum_buffer_min,
            "LodgingRouteUse.minimum_buffer_min",
            maximum=_MAX_BUFFER_MIN,
        )
        expected = _canonical_digest(
            {
                "slot_id": slot_id,
                "day_id": day_id,
                "candidate_id": candidate_id,
                "probe_id": probe_id,
                "minimum_buffer_min": minimum_buffer,
            },
            prefix="lodging-route-use",
        )
        if self.use_id and self.use_id != expected:
            raise ValueError(
                "LodgingRouteUse.use_id does not match content"
            )
        object.__setattr__(self, "slot_id", slot_id)
        object.__setattr__(self, "day_id", day_id)
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "probe_id", probe_id)
        object.__setattr__(self, "minimum_buffer_min", minimum_buffer)
        object.__setattr__(self, "use_id", expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "use_id": self.use_id,
            "slot_id": self.slot_id,
            "day_id": self.day_id,
            "candidate_id": self.candidate_id,
            "probe_id": self.probe_id,
            "has_minimum_buffer": self.minimum_buffer_min > 0,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingItineraryOption:
    """One bounded detached schedule result for a lodging anchor layout."""

    problem: ScheduleProblem = field(repr=False)
    result: ScheduleResult = field(repr=False)
    anchors: tuple[LodgingDayAnchor, ...]
    route_uses: tuple[LodgingRouteUse, ...]
    option_id: str = ""

    def __post_init__(self) -> None:
        if type(self.problem) is not ScheduleProblem:
            raise TypeError(
                "LodgingItineraryOption.problem must be exact ScheduleProblem"
            )
        if type(self.result) is not ScheduleResult:
            raise TypeError(
                "LodgingItineraryOption.result must be exact ScheduleResult"
            )
        if self.result.problem_id != self.problem.problem_id:
            raise ScheduleContractError(
                "STALE_CANDIDATE",
                "Lodging option result belongs to a different problem.",
            )
        anchors = _ordered_anchors(self.anchors)
        route_uses = _ordered_route_uses(self.route_uses)
        candidate_ids = {
            candidate_id
            for anchor in anchors
            for candidate_id in anchor.candidate_ids
        }.union(item.candidate_id for item in route_uses)
        if not candidate_ids:
            raise ValueError(
                "Lodging itinerary option has no lodging candidate"
            )
        if len(candidate_ids) > _MAX_OPTION_CANDIDATES:
            raise ValueError(
                "Lodging itinerary option exceeds the candidate limit"
            )
        expected = _canonical_digest(
            {
                "problem_id": self.problem.problem_id,
                "result_status": self.result.status.value,
                "schedule_candidate_ids": [
                    item.candidate_id for item in self.result.candidates
                ],
                "anchors": [item.to_dict() for item in anchors],
                "route_uses": [item.to_dict() for item in route_uses],
            },
            prefix="lodging-itinerary-option",
        )
        if self.option_id and self.option_id != expected:
            raise ValueError(
                "LodgingItineraryOption.option_id does not match content"
            )
        object.__setattr__(self, "anchors", anchors)
        object.__setattr__(self, "route_uses", route_uses)
        object.__setattr__(self, "option_id", expected)

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    candidate_id
                    for anchor in self.anchors
                    for candidate_id in anchor.candidate_ids
                }.union(
                    item.candidate_id for item in self.route_uses
                )
            )
        )

    def __repr__(self) -> str:
        return (
            "LodgingItineraryOption("
            f"option_id={self.option_id!r}, "
            f"candidate_count={len(self.candidate_ids)!r}, "
            f"schedule_status={self.result.status.value!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "candidate_ids": list(self.candidate_ids),
            "anchors": [item.to_dict() for item in self.anchors],
            "route_uses": [item.to_dict() for item in self.route_uses],
            "schedule_status": self.result.status.value,
            "has_schedule_candidate": self.result.candidate is not None,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingItineraryScore:
    """Transparent exact integer vector; lower objective key is better."""

    hard_violation_count: int
    missing_required_count: int
    protected_change_count: int
    verification_risk_count: int
    split_stay_count: int
    accepted_activity_change_count: int
    accepted_day_move_count: int
    accepted_order_inversion_count: int
    accepted_time_shift_deci_min: int
    served_priority_points: int
    scheduled_optional_count: int
    soft_constraint_violation_count: int
    tight_slack_count: int
    slack_deficit_deci_min: int
    activity_count_overage: int
    service_overage_deci_min: int
    max_lodging_leg_deci_min: int
    wait_deci_min: int
    travel_deci_min: int
    route_slot_count: int
    score_id: str = ""

    def __post_init__(self) -> None:
        non_negative = (
            "hard_violation_count",
            "missing_required_count",
            "protected_change_count",
            "verification_risk_count",
            "split_stay_count",
            "accepted_activity_change_count",
            "accepted_day_move_count",
            "accepted_order_inversion_count",
            "accepted_time_shift_deci_min",
            "scheduled_optional_count",
            "soft_constraint_violation_count",
            "tight_slack_count",
            "slack_deficit_deci_min",
            "activity_count_overage",
            "service_overage_deci_min",
            "max_lodging_leg_deci_min",
            "wait_deci_min",
            "travel_deci_min",
            "route_slot_count",
        )
        for name in non_negative:
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value > 10**12:
                raise ValueError(
                    f"LodgingItineraryScore.{name} must be a bounded "
                    "non-negative integer"
                )
        if (
            type(self.served_priority_points) is not int
            or abs(self.served_priority_points) > 10**12
        ):
            raise ValueError(
                "served_priority_points must be a bounded integer"
            )
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "score_id"
        }
        expected = _canonical_digest(
            payload,
            prefix="lodging-itinerary-score",
        )
        if self.score_id and self.score_id != expected:
            raise ValueError(
                "LodgingItineraryScore.score_id does not match content"
            )
        object.__setattr__(self, "score_id", expected)

    def objective_key(self) -> tuple[int, ...]:
        """Hard constraints and evidence always precede convenience."""

        return (
            self.hard_violation_count,
            self.missing_required_count,
            self.protected_change_count,
            self.verification_risk_count,
            self.split_stay_count,
            self.accepted_activity_change_count,
            self.accepted_day_move_count,
            self.accepted_order_inversion_count,
            self.accepted_time_shift_deci_min,
            -self.served_priority_points,
            self.scheduled_optional_count,
            self.soft_constraint_violation_count,
            self.tight_slack_count,
            self.slack_deficit_deci_min,
            self.activity_count_overage,
            self.service_overage_deci_min,
            self.max_lodging_leg_deci_min,
            self.wait_deci_min,
            self.travel_deci_min,
        )

    def __repr__(self) -> str:
        return f"LodgingItineraryScore(score_id={self.score_id!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted view without provider-derived duration totals."""

        return {
            "score_id": self.score_id,
            "hard_violation_count": self.hard_violation_count,
            "missing_required_count": self.missing_required_count,
            "protected_change_count": self.protected_change_count,
            "verification_risk_count": self.verification_risk_count,
            "split_stay_count": self.split_stay_count,
            "served_priority_points": self.served_priority_points,
            "soft_constraint_violation_count": (
                self.soft_constraint_violation_count
            ),
            "tight_slack_count": self.tight_slack_count,
            "route_slot_count": self.route_slot_count,
            "has_verified_travel_metrics": self.route_slot_count > 0,
        }


@dataclass(frozen=True, slots=True)
class LodgingItineraryIssue:
    """One fixed, secret-free explanation for recommendation readiness."""

    code: str
    severity: IssueSeverity
    message: str
    option_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    slot_ids: tuple[str, ...] = ()
    suggested_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        code = _machine_id(self.code, "LodgingItineraryIssue.code")
        if type(self.message) is not str:
            raise TypeError("LodgingItineraryIssue.message must be text")
        message = unicodedata.normalize("NFC", self.message).strip()
        if (
            not message
            or len(message) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in message)
        ):
            raise ValueError(
                "LodgingItineraryIssue.message must be bounded safe text"
            )
        if type(self.severity) is not IssueSeverity:
            raise TypeError("LodgingItineraryIssue.severity must be exact")
        option_ids = _ordered_digests(self.option_ids, "option_ids")
        candidate_ids = _ordered_digests(
            self.candidate_ids,
            "candidate_ids",
        )
        if not isinstance(self.slot_ids, tuple):
            raise TypeError("slot_ids must be a tuple")
        slot_ids = tuple(
            sorted(
                {
                    _machine_id(item, "slot_ids item")
                    for item in self.slot_ids
                }
            )
        )
        if not isinstance(self.suggested_actions, tuple):
            raise TypeError("suggested_actions must be a tuple")
        actions = tuple(
            sorted(
                {
                    _machine_id(item, "suggested_actions item")
                    for item in self.suggested_actions
                }
            )
        )
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "option_ids", option_ids)
        object.__setattr__(self, "candidate_ids", candidate_ids)
        object.__setattr__(self, "slot_ids", slot_ids)
        object.__setattr__(self, "suggested_actions", actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "option_ids": list(self.option_ids),
            "candidate_ids": list(self.candidate_ids),
            "slot_ids": list(self.slot_ids),
            "suggested_actions": list(self.suggested_actions),
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingOptionAssessment:
    """One option's non-authoritative joint evaluation."""

    option_id: str
    disposition: LodgingOptionDisposition
    candidate_ids: tuple[str, ...]
    issue_codes: tuple[str, ...]
    problem_id: str
    schedule_candidate_id: str | None = None
    score: LodgingItineraryScore | None = field(
        default=None,
        repr=False,
    )
    assessment_id: str = ""

    def __post_init__(self) -> None:
        option_id = _digest(
            self.option_id,
            "LodgingOptionAssessment.option_id",
        )
        if type(self.disposition) is not LodgingOptionDisposition:
            raise TypeError(
                "LodgingOptionAssessment.disposition must be exact"
            )
        candidate_ids = _ordered_digests(
            self.candidate_ids,
            "candidate_ids",
        )
        if not candidate_ids:
            raise ValueError(
                "LodgingOptionAssessment requires candidate IDs"
            )
        if not isinstance(self.issue_codes, tuple):
            raise TypeError("issue_codes must be a tuple")
        issue_codes = tuple(
            sorted(
                {
                    _machine_id(item, "issue_codes item")
                    for item in self.issue_codes
                }
            )
        )
        problem_id = _schedule_digest(
            self.problem_id,
            "LodgingOptionAssessment.problem_id",
        )
        schedule_id = (
            _schedule_digest(
                self.schedule_candidate_id,
                "LodgingOptionAssessment.schedule_candidate_id",
            )
            if self.schedule_candidate_id is not None
            else None
        )
        if self.disposition is LodgingOptionDisposition.COMPARABLE:
            if (
                type(self.score) is not LodgingItineraryScore
                or schedule_id is None
            ):
                raise ValueError(
                    "Comparable option requires a replayed score"
                )
        elif self.score is not None:
            raise ValueError(
                "Non-comparable option cannot expose numeric score"
            )
        expected = _canonical_digest(
            {
                "option_id": option_id,
                "disposition": self.disposition.value,
                "candidate_ids": list(candidate_ids),
                "issue_codes": list(issue_codes),
                "problem_id": problem_id,
                "schedule_candidate_id": schedule_id,
                "score_id": (
                    self.score.score_id
                    if self.score is not None
                    else None
                ),
            },
            prefix="lodging-option-assessment",
        )
        if self.assessment_id and self.assessment_id != expected:
            raise ValueError(
                "LodgingOptionAssessment.assessment_id differs from content"
            )
        object.__setattr__(self, "option_id", option_id)
        object.__setattr__(self, "candidate_ids", candidate_ids)
        object.__setattr__(self, "issue_codes", issue_codes)
        object.__setattr__(self, "problem_id", problem_id)
        object.__setattr__(self, "schedule_candidate_id", schedule_id)
        object.__setattr__(self, "assessment_id", expected)

    @property
    def supports_authoritative_use(self) -> bool:
        return False

    def __repr__(self) -> str:
        return (
            "LodgingOptionAssessment("
            f"assessment_id={self.assessment_id!r}, "
            f"disposition={self.disposition.value!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "option_id": self.option_id,
            "disposition": self.disposition.value,
            "candidate_ids": list(self.candidate_ids),
            "issue_codes": list(self.issue_codes),
            "problem_id": self.problem_id,
            "schedule_candidate_id": self.schedule_candidate_id,
            "score": self.score.to_dict() if self.score is not None else None,
            "supports_authoritative_use": False,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingItineraryAssessment:
    """Bounded recommendation result with no apply or promotion capability."""

    status: LodgingItineraryStatus
    basis: LodgingEvidenceBasis = field(repr=False)
    stay_start: date = field(repr=False)
    stay_end: date = field(repr=False)
    options: tuple[LodgingOptionAssessment, ...] = ()
    issues: tuple[LodgingItineraryIssue, ...] = ()
    review_order: tuple[str, ...] = ()
    priority_review_option_id: str | None = None
    assessment_id: str = ""
    contract_version: str = LODGING_ITINERARY_VERSION

    def __post_init__(self) -> None:
        if type(self.status) is not LodgingItineraryStatus:
            raise TypeError(
                "LodgingItineraryAssessment.status must be exact"
            )
        if type(self.basis) is not LodgingEvidenceBasis:
            raise TypeError(
                "LodgingItineraryAssessment.basis must be exact"
            )
        stay_start = _exact_date(self.stay_start, "stay_start")
        stay_end = _exact_date(self.stay_end, "stay_end")
        if (
            stay_end <= stay_start
            or (stay_end - stay_start).days > _MAX_DAY_ANCHORS
        ):
            raise ValueError(
                "Lodging itinerary stay span is outside the bounded range"
            )
        if (
            not isinstance(self.options, tuple)
            or any(
                type(item) is not LodgingOptionAssessment
                for item in self.options
            )
        ):
            raise TypeError(
                "options must contain exact LodgingOptionAssessment values"
            )
        options = tuple(
            sorted(self.options, key=lambda item: item.option_id)
        )
        option_ids = tuple(item.option_id for item in options)
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("options cannot contain duplicates")
        if (
            not isinstance(self.issues, tuple)
            or any(
                type(item) is not LodgingItineraryIssue
                for item in self.issues
            )
        ):
            raise TypeError(
                "issues must contain exact LodgingItineraryIssue values"
            )
        issues = tuple(
            sorted(
                self.issues,
                key=lambda item: (
                    item.code,
                    item.option_ids,
                    item.candidate_ids,
                    item.slot_ids,
                ),
            )
        )
        review_order = _ordered_digests(
            self.review_order,
            "review_order",
            preserve_order=True,
        )
        if not set(review_order).issubset(option_ids):
            raise ValueError("review_order references an unknown option")
        priority = (
            _digest(
                self.priority_review_option_id,
                "priority_review_option_id",
            )
            if self.priority_review_option_id is not None
            else None
        )
        if priority is not None and priority not in review_order:
            raise ValueError(
                "priority review option must be in review_order"
            )
        if self.status is LodgingItineraryStatus.NO_LODGING_OPTIONS:
            valid_shape = not options and not review_order and priority is None
        elif self.status is LodgingItineraryStatus.RANKED:
            valid_shape = bool(options and review_order and priority)
        elif self.status is LodgingItineraryStatus.NOT_RANKED:
            valid_shape = bool(options and review_order) and priority is None
        else:
            valid_shape = bool(options) and not review_order and priority is None
        if not valid_shape:
            raise ValueError(
                "Lodging itinerary status and recommendation shape disagree"
            )
        if self.contract_version != LODGING_ITINERARY_VERSION:
            raise ValueError("Unsupported lodging itinerary version")
        expected = _canonical_digest(
            {
                "contract_version": self.contract_version,
                "status": self.status.value,
                "basis_id": self.basis.basis_id,
                "stay_start": stay_start.isoformat(),
                "stay_end": stay_end.isoformat(),
                "option_assessment_ids": [
                    item.assessment_id for item in options
                ],
                "issues": [item.to_dict() for item in issues],
                "review_order": list(review_order),
                "priority_review_option_id": priority,
            },
            prefix="lodging-itinerary-assessment",
        )
        if self.assessment_id and self.assessment_id != expected:
            raise ValueError(
                "LodgingItineraryAssessment.assessment_id differs from content"
            )
        object.__setattr__(self, "stay_start", stay_start)
        object.__setattr__(self, "stay_end", stay_end)
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "issues", issues)
        object.__setattr__(self, "review_order", review_order)
        object.__setattr__(
            self,
            "priority_review_option_id",
            priority,
        )
        object.__setattr__(self, "assessment_id", expected)

    @property
    def supports_authoritative_use(self) -> bool:
        return False

    @property
    def needs_verification(self) -> bool:
        return self.status in {
            LodgingItineraryStatus.NEEDS_VERIFICATION,
            LodgingItineraryStatus.NOT_RANKED,
        }

    def require_snapshot(self, snapshot: EvidenceSnapshot) -> None:
        if not self.basis.matches(snapshot):
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                "Lodging itinerary evidence snapshot changed.",
            )

    def __repr__(self) -> str:
        return (
            "LodgingItineraryAssessment("
            f"assessment_id={self.assessment_id!r}, "
            f"status={self.status.value!r}, "
            f"option_count={len(self.options)!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "status": self.status.value,
            "basis_id": self.basis.basis_id,
            "stay_night_count": (self.stay_end - self.stay_start).days,
            "options": [item.to_dict() for item in self.options],
            "issues": [item.to_dict() for item in self.issues],
            "review_order": list(self.review_order),
            "priority_review_option_id": self.priority_review_option_id,
            "needs_verification": self.needs_verification,
            "supports_authoritative_use": False,
            "contract_version": self.contract_version,
        }


def _ordered_anchors(
    anchors: tuple[LodgingDayAnchor, ...],
) -> tuple[LodgingDayAnchor, ...]:
    if (
        not isinstance(anchors, tuple)
        or any(type(item) is not LodgingDayAnchor for item in anchors)
    ):
        raise TypeError("anchors must contain exact LodgingDayAnchor values")
    if not anchors or len(anchors) > _MAX_DAY_ANCHORS:
        raise ValueError("anchors are outside the bounded option contract")
    ordered = tuple(sorted(anchors, key=lambda item: item.day_id))
    if len({item.day_id for item in ordered}) != len(ordered):
        raise ValueError("anchors cannot repeat a planning day")
    return ordered


def _ordered_route_uses(
    values: tuple[LodgingRouteUse, ...],
) -> tuple[LodgingRouteUse, ...]:
    if (
        not isinstance(values, tuple)
        or any(type(item) is not LodgingRouteUse for item in values)
    ):
        raise TypeError(
            "route_uses must contain exact LodgingRouteUse values"
        )
    if len(values) > _MAX_ROUTE_USES:
        raise ValueError("route_uses exceed the bounded option contract")
    ordered = tuple(
        sorted(values, key=lambda item: (item.slot_id, item.use_id))
    )
    if len({item.slot_id for item in ordered}) != len(ordered):
        raise ValueError("route_uses cannot repeat a semantic slot")
    if len({item.probe_id for item in ordered}) != len(ordered):
        raise ValueError("route_uses cannot repeat a route probe")
    return ordered


def _ordered_digests(
    values: tuple[str, ...],
    name: str,
    *,
    preserve_order: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    normalized = tuple(_digest(item, f"{name} item") for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} cannot contain duplicates")
    return normalized if preserve_order else tuple(sorted(normalized))


def _snapshot_matches_binding(
    snapshot: EvidenceSnapshot,
    binding: EvidenceBinding,
) -> bool:
    return (
        snapshot.policies.revision == binding.policy_registry_revision
        and snapshot.store_revision == binding.store_revision
        and snapshot.evidence_revision == binding.evidence_revision
        and snapshot.evaluation_at == binding.evaluation_at
        and snapshot.purge_checked_at == binding.purge_checked_at
        and snapshot.outcome_revision == binding.outcome_revision
        and snapshot.snapshot_id == binding.snapshot_id
    )


def _require_composed_snapshot(
    composed: ComposedTripState,
    snapshot: EvidenceSnapshot,
) -> None:
    if type(composed) is not ComposedTripState:
        raise TypeError("composed must be exact ComposedTripState")
    if type(snapshot) is not EvidenceSnapshot:
        raise TypeError("snapshot must be exact EvidenceSnapshot")
    if not _snapshot_matches_binding(snapshot, composed.evidence):
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Composed itinerary and lodging evidence snapshots differ.",
        )


def build_lodging_itinerary_problem(
    *,
    composed: ComposedTripState,
    comparison: LodgingComparisonAssessment,
    snapshot: EvidenceSnapshot,
    anchors: tuple[LodgingDayAnchor, ...],
    scope: ReplanScope | None = None,
    preferences: SchedulePreferences | None = None,
    limits: SearchLimits | None = None,
) -> ScheduleProblem:
    """Build a detached anchor what-if; canonical state remains untouched."""

    if type(comparison) is not LodgingComparisonAssessment:
        raise TypeError(
            "comparison must be exact LodgingComparisonAssessment"
        )
    comparison.require_snapshot(snapshot)
    _require_composed_snapshot(composed, snapshot)
    ordered_anchors = _ordered_anchors(anchors)
    comparison_by_id = {
        item.candidate_id: item for item in comparison.candidates
    }
    detached_state = _detached_state_for_anchors(
        composed.state,
        ordered_anchors,
        comparison_by_id,
    )
    detached = ComposedTripState(
        state=detached_state,
        trip_id=composed.trip_id,
        plan_revision=composed.plan_revision,
        canonical_state_digest=composed.canonical_state_digest,
        composed_state_digest=trip_state_digest(detached_state),
        evidence=composed.evidence,
        live_attributions=composed.live_attributions,
        activity_availability=composed.activity_availability,
    )
    return schedule_problem_from_composed(
        detached,
        scope=scope,
        preferences=preferences,
        limits=limits,
    )


def assess_lodging_itineraries(
    *,
    composed: ComposedTripState,
    comparison: LodgingComparisonAssessment,
    snapshot: EvidenceSnapshot,
    stay_start: date,
    stay_end: date,
    options: tuple[LodgingItineraryOption, ...],
) -> LodgingItineraryAssessment:
    """Rank only jointly feasible options with one common evidence basis."""

    if type(comparison) is not LodgingComparisonAssessment:
        raise TypeError(
            "comparison must be exact LodgingComparisonAssessment"
        )
    if type(snapshot) is not EvidenceSnapshot:
        raise TypeError("snapshot must be exact EvidenceSnapshot")
    comparison.require_snapshot(snapshot)
    _require_composed_snapshot(composed, snapshot)
    stay_start = _exact_date(stay_start, "stay_start")
    stay_end = _exact_date(stay_end, "stay_end")
    if (
        stay_end <= stay_start
        or (stay_end - stay_start).days > _MAX_DAY_ANCHORS
    ):
        raise ValueError(
            "Lodging itinerary stay span is outside the bounded range"
        )
    if (
        not isinstance(options, tuple)
        or any(type(item) is not LodgingItineraryOption for item in options)
    ):
        raise TypeError(
            "options must contain exact LodgingItineraryOption values"
        )
    if len(options) > _MAX_OPTIONS:
        raise ValueError("Lodging itinerary option limit exceeded")
    if not options:
        return LodgingItineraryAssessment(
            status=LodgingItineraryStatus.NO_LODGING_OPTIONS,
            basis=comparison.basis,
            stay_start=stay_start,
            stay_end=stay_end,
        )

    ordered_options = tuple(
        sorted(options, key=lambda item: item.option_id)
    )
    if len({item.option_id for item in ordered_options}) != len(
        ordered_options
    ):
        raise ValueError("Lodging itinerary options cannot contain duplicates")
    comparison_by_id = {
        item.candidate_id: item for item in comparison.candidates
    }
    used_candidate_ids = {
        candidate_id
        for option in ordered_options
        for candidate_id in option.candidate_ids
    }
    if used_candidate_ids != set(comparison_by_id):
        raise ValueError(
            "Every compared lodging candidate must appear in an option"
        )
    _validate_option_family(
        ordered_options,
        snapshot,
        composed,
        comparison_by_id,
    )

    option_assessments: list[LodgingOptionAssessment] = []
    issues: list[LodgingItineraryIssue] = []
    for option in ordered_options:
        assessment, option_issues = _assess_option(
            option=option,
            comparison_by_id=comparison_by_id,
            comparison=comparison,
            snapshot=snapshot,
            stay_start=stay_start,
            stay_end=stay_end,
        )
        option_assessments.append(assessment)
        issues.extend(option_issues)
        if len(issues) > _MAX_ISSUES:
            raise ValueError("Lodging itinerary issue limit exceeded")

    issues.append(
        LodgingItineraryIssue(
            code="LODGING_PRICE_NOT_SCORED",
            severity=IssueSeverity.INFO,
            message=(
                "Lodging price is excluded until comparable scoped price "
                "evidence exists."
            ),
            option_ids=tuple(
                item.option_id for item in ordered_options
            ),
            candidate_ids=tuple(sorted(comparison_by_id)),
            suggested_actions=("verify_comparable_lodging_prices",),
        )
    )
    assessments = tuple(option_assessments)
    if any(
        item.disposition is LodgingOptionDisposition.NEEDS_VERIFICATION
        for item in assessments
    ):
        status = LodgingItineraryStatus.NEEDS_VERIFICATION
        review_order: tuple[str, ...] = ()
        priority = None
    else:
        comparable = tuple(
            item
            for item in assessments
            if item.disposition is LodgingOptionDisposition.COMPARABLE
        )
        if not comparable:
            status = LodgingItineraryStatus.INFEASIBLE
            review_order = ()
            priority = None
        else:
            ranked = tuple(
                sorted(
                    comparable,
                    key=lambda item: (
                        item.score.objective_key()
                        if item.score is not None
                        else (),
                        item.option_id,
                    ),
                )
            )
            review_order = tuple(item.option_id for item in ranked)
            assert ranked[0].score is not None
            best_key = ranked[0].score.objective_key()
            tied = sum(
                item.score is not None
                and item.score.objective_key() == best_key
                for item in ranked
            )
            if tied > 1:
                status = LodgingItineraryStatus.NOT_RANKED
                priority = None
                issues.append(
                    LodgingItineraryIssue(
                        code="LODGING_OPTIONS_TIED",
                        severity=IssueSeverity.INFO,
                        message=(
                            "Top lodging itineraries are tied on the "
                            "lexicographic recommendation vector."
                        ),
                        option_ids=tuple(
                            item.option_id
                            for item in ranked
                            if item.score is not None
                            and item.score.objective_key() == best_key
                        ),
                        suggested_actions=(
                            "ask_lodging_tie_break_preference",
                        ),
                    )
                )
            else:
                status = LodgingItineraryStatus.RANKED
                priority = ranked[0].option_id

    return LodgingItineraryAssessment(
        status=status,
        basis=comparison.basis,
        stay_start=stay_start,
        stay_end=stay_end,
        options=assessments,
        issues=tuple(issues),
        review_order=review_order,
        priority_review_option_id=priority,
    )


def _validate_option_family(
    options: tuple[LodgingItineraryOption, ...],
    snapshot: EvidenceSnapshot,
    composed: ComposedTripState,
    comparison_by_id: dict[str, LodgingComparisonCandidate],
) -> None:
    first = options[0]
    first_problem = first.problem
    first_signature = (
        first_problem.trip_id,
        first_problem.base_revision,
        first_problem.canonical_state_digest,
        _problem_core_digest(first_problem.state),
        first_problem.evaluation_at,
        first_problem.scope,
        first_problem.preferences,
        first_problem.limits,
        first_problem.activity_availability,
        tuple(
            (
                item.day_id,
                item.start_candidate_id is not None,
                item.end_candidate_id is not None,
            )
            for item in first.anchors
        ),
        tuple(
            (
                item.slot_id,
                item.day_id,
                item.minimum_buffer_min,
            )
            for item in first.route_uses
        ),
    )
    for option in options:
        _require_problem_snapshot(option.problem, snapshot)
        problem = option.problem
        if (
            problem.trip_id != composed.trip_id
            or problem.base_revision != composed.plan_revision
            or problem.canonical_state_digest
            != composed.canonical_state_digest
            or problem.evidence_binding != composed.evidence
            or problem.activity_availability
            != composed.activity_availability
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Lodging option is not derived from the composed itinerary.",
            )
        expected_state = _detached_state_for_anchors(
            composed.state,
            option.anchors,
            comparison_by_id,
        )
        if problem.state != expected_state:
            raise ScheduleContractError(
                "INVALID_INPUT",
                "Lodging option contains an undeclared itinerary mutation.",
            )
        _validate_problem_availability(problem, snapshot)
        signature = (
            problem.trip_id,
            problem.base_revision,
            problem.canonical_state_digest,
            _problem_core_digest(problem.state),
            problem.evaluation_at,
            problem.scope,
            problem.preferences,
            problem.limits,
            problem.activity_availability,
            tuple(
                (
                    item.day_id,
                    item.start_candidate_id is not None,
                    item.end_candidate_id is not None,
                )
                for item in option.anchors
            ),
            tuple(
                (
                    item.slot_id,
                    item.day_id,
                    item.minimum_buffer_min,
                )
                for item in option.route_uses
            ),
        )
        if signature != first_signature:
            raise ValueError(
                "Lodging options do not share one comparable itinerary family"
            )
    _validate_route_slot_family(options, comparison_by_id)


def _detached_state_for_anchors(
    state: TripState,
    anchors: tuple[LodgingDayAnchor, ...],
    comparison_by_id: dict[str, LodgingComparisonCandidate],
) -> TripState:
    days = {item.day_id: item for item in state.days}
    replacements: dict[str, Any] = {}
    for anchor in anchors:
        if anchor.day_id not in days:
            raise ValueError("Lodging anchor references an unknown day")
        changes: dict[str, str] = {}
        for field_name, candidate_id in (
            ("start_location_id", anchor.start_candidate_id),
            ("end_location_id", anchor.end_candidate_id),
        ):
            if candidate_id is None:
                continue
            candidate = comparison_by_id.get(candidate_id)
            if candidate is None:
                raise ValueError(
                    "Lodging anchor references an unknown candidate"
                )
            endpoint = candidate.identity.endpoint
            if endpoint is None:
                raise FactContractError(
                    "LODGING_IDENTITY_REQUIRED",
                    "Lodging anchor needs fresh identity evidence.",
                )
            changes[field_name] = endpoint.location_id
        replacements[anchor.day_id] = replace(
            days[anchor.day_id],
            **changes,
        )
    return replace(
        state,
        days=tuple(
            replacements.get(day.day_id, day)
            for day in state.days
        ),
    )


def _validate_route_slot_family(
    options: tuple[LodgingItineraryOption, ...],
    comparison_by_id: dict[str, LodgingComparisonCandidate],
) -> None:
    by_slot: dict[str, tuple[Any, ...]] = {}
    for option in options:
        for route_use in option.route_uses:
            route = _route_by_probe(
                comparison_by_id.get(route_use.candidate_id),
                route_use.probe_id,
            )
            if route is None:
                continue
            probe = route.probe
            signature = (
                route_use.day_id,
                route_use.minimum_buffer_min,
                probe.direction,
                probe.mode,
                probe.anchor_location_id,
                probe.departure_at,
            )
            previous = by_slot.setdefault(route_use.slot_id, signature)
            if previous != signature:
                raise ValueError(
                    "Lodging route slots do not describe equivalent uses"
                )


def _validate_problem_availability(
    problem: ScheduleProblem,
    snapshot: EvidenceSnapshot,
) -> None:
    projected, _used = project_activity_availability(
        problem.state,
        snapshot,
    )
    expected_by_id = {
        item.activity_id: item
        for item in projected
    }
    actual_by_id = {
        item.activity_id: item
        for item in problem.activity_availability
    }
    for activity_id, expected in expected_by_id.items():
        actual = actual_by_id.get(activity_id)
        if (
            actual is None
            or actual.disposition is not expected.disposition
            or (
                expected.disposition
                is AvailabilityDisposition.HARD_CURRENT
                and actual != expected
            )
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Activity availability differs from the current snapshot.",
            )
    for actual in problem.activity_availability:
        if (
            actual.disposition is AvailabilityDisposition.HARD_CURRENT
            and expected_by_id.get(actual.activity_id) != actual
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Hard activity availability lacks current snapshot evidence.",
            )
        for reference in actual.evidence_refs:
            if (
                reference.startswith("fact:")
                and reference[5:]
                not in problem.evidence_binding.used_observation_ids
            ):
                raise FactContractError(
                    "EVIDENCE_BINDING_MISMATCH",
                    "Activity availability evidence is absent from the "
                    "schedule binding.",
                )


def _problem_core_digest(state: TripState) -> str:
    stripped = replace(
        state,
        days=tuple(
            replace(
                day,
                start_location_id=None,
                end_location_id=None,
            )
            for day in state.days
        ),
        travel_estimates=(),
        load_issues=(),
    )
    return trip_state_digest(stripped)


def _require_problem_snapshot(
    problem: ScheduleProblem,
    snapshot: EvidenceSnapshot,
) -> EvidenceBinding:
    binding = problem.evidence_binding
    if binding is None:
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Lodging itinerary scoring requires an evidence-bound problem.",
        )
    if not _snapshot_matches_binding(snapshot, binding):
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Schedule problem and lodging evidence snapshots differ.",
        )
    if problem.evaluation_at != snapshot.evaluation_at:
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Schedule and evidence evaluation clocks differ.",
        )
    return binding


def _assess_option(
    *,
    option: LodgingItineraryOption,
    comparison_by_id: dict[str, LodgingComparisonCandidate],
    comparison: LodgingComparisonAssessment,
    snapshot: EvidenceSnapshot,
    stay_start: date,
    stay_end: date,
) -> tuple[LodgingOptionAssessment, tuple[LodgingItineraryIssue, ...]]:
    binding = _require_problem_snapshot(option.problem, snapshot)
    comparisons = tuple(
        comparison_by_id[candidate_id]
        for candidate_id in option.candidate_ids
    )
    issues: list[LodgingItineraryIssue] = []
    hard_coverage_error = _coverage_issue(
        option,
        comparisons,
        stay_start,
        stay_end,
    )
    if hard_coverage_error is not None:
        issues.append(hard_coverage_error)
        return _option_assessment(
            option,
            LodgingOptionDisposition.INFEASIBLE,
            issues,
        ), tuple(issues)

    pending_claims = tuple(
        item.candidate_id
        for item in comparisons
        if item.candidate.draft.reported_decision is not None
    )
    if pending_claims:
        issues.append(
            _option_issue(
                option,
                "REPORTED_LODGING_DECISION_AWAITS_CONFIRMATION",
                IssueSeverity.WARNING,
                (
                    "A reported lodging decision awaits the dedicated "
                    "confirmation boundary."
                ),
                candidate_ids=pending_claims,
                actions=("confirm_reported_lodging_decision",),
            )
        )

    missing_identity = tuple(
        item.candidate_id
        for item in comparisons
        if item.identity.endpoint is None
    )
    if missing_identity:
        issues.append(
            _option_issue(
                option,
                "LODGING_IDENTITY_REQUIRED",
                IssueSeverity.WARNING,
                "Every lodging anchor needs fresh identity evidence.",
                candidate_ids=missing_identity,
                actions=("verify_lodging_location",),
            )
        )
    _validate_option_anchors(
        option,
        comparison_by_id,
    )

    route_durations: list[int] = []
    for route_use in option.route_uses:
        comparison_candidate = comparison_by_id.get(
            route_use.candidate_id
        )
        route = (
            _route_by_probe(
                comparison_candidate,
                route_use.probe_id,
            )
            if comparison_candidate is not None
            else None
        )
        if (
            route is None
            or route.disposition is not LodgingRouteDisposition.VERIFIED
            or route.duration_min is None
            or route.used_observation_id is None
        ):
            issues.append(
                _option_issue(
                    option,
                    "LODGING_ROUTE_EVIDENCE_REQUIRED",
                    IssueSeverity.WARNING,
                    (
                        "Every comparison slot needs a fresh route receipt "
                        "on the current snapshot."
                    ),
                    candidate_ids=(route_use.candidate_id,),
                    slot_ids=(route_use.slot_id,),
                    actions=("refresh_lodging_route",),
                )
            )
            continue
        estimate = _route_use_estimate(
            route_use,
            route,
            option.problem,
            snapshot,
            binding,
        )
        if estimate is None:
            issues.append(
                _option_issue(
                    option,
                    "LODGING_ROUTE_PROJECTION_REQUIRED",
                    IssueSeverity.WARNING,
                    (
                        "A verified lodging route is not bound into the "
                        "detached itinerary projection."
                    ),
                    candidate_ids=(route_use.candidate_id,),
                    slot_ids=(route_use.slot_id,),
                    actions=("recompose_lodging_itinerary",),
                )
            )
            continue
        if float(estimate.buffer_min) < route_use.minimum_buffer_min:
            issues.append(
                _option_issue(
                    option,
                    "LODGING_TRANSFER_BUFFER_INSUFFICIENT",
                    IssueSeverity.ERROR,
                    (
                        "A lodging transfer does not meet its explicit "
                        "planning buffer."
                    ),
                    candidate_ids=(route_use.candidate_id,),
                    slot_ids=(route_use.slot_id,),
                    actions=("increase_transfer_buffer",),
                )
            )
            continue
        route_durations.append(
            _deci(
                route.duration_min,
                "verified lodging route duration",
            )
        )

    if not option.route_uses:
        issues.append(
            _option_issue(
                option,
                "LODGING_ROUTE_SLOTS_MISSING",
                IssueSeverity.WARNING,
                "Joint comparison requires explicit equivalent route slots.",
                actions=("add_lodging_route_slots",),
            )
        )

    if any(item.severity is IssueSeverity.ERROR for item in issues):
        return _option_assessment(
            option,
            LodgingOptionDisposition.INFEASIBLE,
            issues,
        ), tuple(issues)
    if pending_claims or missing_identity or any(
        item.severity is IssueSeverity.WARNING for item in issues
    ):
        return _option_assessment(
            option,
            LodgingOptionDisposition.NEEDS_VERIFICATION,
            issues,
        ), tuple(issues)

    if option.result.status is ScheduleStatus.PROVEN_INFEASIBLE:
        issues.append(
            _option_issue(
                option,
                "JOINT_ITINERARY_INFEASIBLE",
                IssueSeverity.ERROR,
                (
                    "The lodging anchor layout cannot satisfy the protected "
                    "itinerary constraints."
                ),
                actions=("review_lodging_anchor_layout",),
            )
        )
        return _option_assessment(
            option,
            LodgingOptionDisposition.INFEASIBLE,
            issues,
        ), tuple(issues)
    if option.result.status is not ScheduleStatus.SOLVED:
        issues.append(
            _option_issue(
                option,
                "JOINT_ITINERARY_NEEDS_VERIFICATION",
                IssueSeverity.WARNING,
                (
                    "The lodging itinerary has no complete replayed schedule "
                    "under the current evidence."
                ),
                actions=("refresh_or_recompute_itinerary",),
            )
        )
        return _option_assessment(
            option,
            LodgingOptionDisposition.NEEDS_VERIFICATION,
            issues,
        ), tuple(issues)

    schedule_candidate = option.result.candidate
    assert schedule_candidate is not None
    replay_schedule_candidate(option.problem, schedule_candidate)
    route_issue = _required_arc_issue(
        option,
        schedule_candidate,
        snapshot,
        binding,
    )
    if route_issue is not None:
        issues.append(route_issue)
        return _option_assessment(
            option,
            LodgingOptionDisposition.NEEDS_VERIFICATION,
            issues,
        ), tuple(issues)

    schedule_score = schedule_candidate.score
    if (
        schedule_score.hard_violation_count
        or schedule_score.missing_required_count
        or schedule_score.protected_change_count
    ):
        issues.append(
            _option_issue(
                option,
                "JOINT_ITINERARY_PROTECTED_CONSTRAINT_FAILED",
                IssueSeverity.ERROR,
                (
                    "The what-if itinerary changes or violates a protected "
                    "constraint."
                ),
                actions=("review_protected_itinerary_constraints",),
            )
        )
        return _option_assessment(
            option,
            LodgingOptionDisposition.INFEASIBLE,
            issues,
        ), tuple(issues)
    if schedule_score.verification_risk_count:
        issues.append(
            _option_issue(
                option,
                "JOINT_ITINERARY_EVIDENCE_REQUIRED",
                IssueSeverity.WARNING,
                (
                    "The what-if itinerary still contains unresolved route "
                    "or opening-hours evidence."
                ),
                actions=("refresh_itinerary_evidence",),
            )
        )
        return _option_assessment(
            option,
            LodgingOptionDisposition.NEEDS_VERIFICATION,
            issues,
        ), tuple(issues)

    score = _joint_score(
        comparisons,
        schedule_score,
        route_durations,
        len(option.route_uses),
    )
    return LodgingOptionAssessment(
        option_id=option.option_id,
        disposition=LodgingOptionDisposition.COMPARABLE,
        candidate_ids=option.candidate_ids,
        issue_codes=(),
        problem_id=option.problem.problem_id,
        schedule_candidate_id=schedule_candidate.candidate_id,
        score=score,
    ), tuple(issues)


def _coverage_issue(
    option: LodgingItineraryOption,
    comparisons: tuple[LodgingComparisonCandidate, ...],
    stay_start: date,
    stay_end: date,
) -> LodgingItineraryIssue | None:
    nights = tuple(
        stay_start + timedelta(days=offset)
        for offset in range((stay_end - stay_start).days)
    )
    if any(
        item.candidate.check_in < stay_start
        or item.candidate.check_out > stay_end
        for item in comparisons
    ):
        return _option_issue(
            option,
            "LODGING_COVERAGE_OUTSIDE_SCOPE",
            IssueSeverity.ERROR,
            "A lodging candidate extends outside the compared stay scope.",
            actions=("align_lodging_coverage",),
        )
    missing = False
    overlap = False
    used: set[str] = set()
    for night in nights:
        covering = tuple(
            item
            for item in comparisons
            if item.candidate.covers(night)
        )
        missing = missing or not covering
        overlap = overlap or len(covering) > 1
        used.update(item.candidate_id for item in covering)
    if missing:
        return _option_issue(
            option,
            "LODGING_COVERAGE_MISSING",
            IssueSeverity.ERROR,
            "The lodging option does not cover every required night.",
            actions=("complete_lodging_coverage",),
        )
    if overlap:
        return _option_issue(
            option,
            "LODGING_COVERAGE_OVERLAP",
            IssueSeverity.ERROR,
            "The lodging option assigns multiple stays to one night.",
            actions=("resolve_lodging_overlap",),
        )
    if used != set(option.candidate_ids):
        return _option_issue(
            option,
            "LODGING_CANDIDATE_UNUSED",
            IssueSeverity.ERROR,
            "The lodging option contains a candidate outside its night plan.",
            actions=("remove_unused_lodging_candidate",),
        )
    return None


def _validate_option_anchors(
    option: LodgingItineraryOption,
    comparison_by_id: dict[str, LodgingComparisonCandidate],
) -> None:
    day_by_id = option.problem.state.day_by_id
    for anchor in option.anchors:
        day = day_by_id.get(anchor.day_id)
        if day is None:
            raise ValueError("Lodging option anchor references an unknown day")
        for candidate_id, actual in (
            (anchor.start_candidate_id, day.start_location_id),
            (anchor.end_candidate_id, day.end_location_id),
        ):
            if candidate_id is None:
                continue
            candidate = comparison_by_id.get(candidate_id)
            if candidate is None or candidate.identity.endpoint is None:
                continue
            if actual != candidate.identity.endpoint.location_id:
                raise FactContractError(
                    "EVIDENCE_BINDING_MISMATCH",
                    "Detached day anchor differs from lodging identity.",
                )


def _route_by_probe(
    candidate: LodgingComparisonCandidate | None,
    probe_id: str,
) -> LodgingRouteEvidence | None:
    if candidate is None:
        return None
    matches = tuple(
        item for item in candidate.routes if item.probe.probe_id == probe_id
    )
    if len(matches) > 1:
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            "Lodging comparison contains duplicate route probes.",
        )
    return matches[0] if matches else None


def _route_use_estimate(
    route_use: LodgingRouteUse,
    route: LodgingRouteEvidence,
    problem: ScheduleProblem,
    snapshot: EvidenceSnapshot,
    binding: EvidenceBinding,
) -> TravelEstimate | None:
    receipt = route.probe.basis_request
    observation_id = route.used_observation_id
    if receipt is None or observation_id is None:
        return None
    reference = f"fact:{observation_id}"
    candidates = tuple(
        estimate
        for estimate in problem.state.travel_estimates
        if estimate.from_location_id == receipt.origin.location_id
        and estimate.to_location_id == receipt.destination.location_id
        and estimate.mode == receipt.mode.value
        and estimate.day_id in (None, route_use.day_id)
        and estimate.evidence_ref == reference
    )
    valid = tuple(
        estimate
        for estimate in candidates
        if _estimate_observation(
            estimate,
            snapshot,
            binding,
        )
        is not None
        and _deci(
            estimate.duration_min,
            "projected lodging route duration",
        )
        == _deci(route.duration_min, "lodging route duration")
    )
    if len(valid) != 1:
        return None
    return valid[0]


def _required_arc_issue(
    option: LodgingItineraryOption,
    candidate: ScheduleCandidate,
    snapshot: EvidenceSnapshot,
    binding: EvidenceBinding,
) -> LodgingItineraryIssue | None:
    for encoded in candidate.required_arc_keys:
        try:
            item = json.loads(encoded)
            day_id = _machine_id(item["day_id"], "required arc day")
            from_location = item["from_location_id"]
            to_location = item["to_location_id"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ScheduleContractError(
                "INVALID_CANDIDATE",
                "Schedule candidate has an invalid required arc.",
            ) from exc
        matching = tuple(
            estimate
            for estimate in option.problem.state.travel_estimates
            if estimate.from_location_id == from_location
            and estimate.to_location_id == to_location
            and estimate.day_id in (None, day_id)
            and estimate.evidence_state is EvidenceState.VERIFIED
        )
        valid = tuple(
            estimate
            for estimate in matching
            if _estimate_observation(
                estimate,
                snapshot,
                binding,
            )
            is not None
        )
        if not valid or len(valid) != len(matching):
            return _option_issue(
                option,
                "ITINERARY_ROUTE_EVIDENCE_REQUIRED",
                IssueSeverity.WARNING,
                (
                    "Every route used by the joint schedule needs fresh "
                    "evidence on the same snapshot."
                ),
                actions=("refresh_itinerary_routes",),
            )
    return None


def _estimate_observation(
    estimate: TravelEstimate,
    snapshot: EvidenceSnapshot,
    binding: EvidenceBinding,
) -> FactObservation | None:
    reference = estimate.evidence_ref
    if (
        estimate.evidence_state is not EvidenceState.VERIFIED
        or type(reference) is not str
        or not reference.startswith("fact:")
        or _DIGEST_RE.fullmatch(reference[5:]) is None
        or estimate.source != reference
    ):
        return None
    observation_id = reference[5:]
    if observation_id not in binding.used_observation_ids:
        return None
    observation = next(
        (
            item
            for item in snapshot.observations
            if item.observation_id == observation_id
        ),
        None,
    )
    if (
        observation is None
        or observation.key.kind is not FactKind.ROUTE_ESTIMATE
        or observation.key.subject_ids
        != (
            estimate.from_location_id,
            estimate.to_location_id,
        )
        or not observation.fresh_at(snapshot.evaluation_at)
        or estimate.fresh_until != observation.valid_until
    ):
        return None
    payload = observation.value.payload
    if payload.get("mode") != estimate.mode:
        return None
    try:
        projected = Decimal(str(estimate.duration_min))
        observed = Decimal(str(payload["duration_min"]))
    except (InvalidOperation, KeyError):
        return None
    if (
        not projected.is_finite()
        or not observed.is_finite()
        or projected != observed
    ):
        return None
    qualifiers = observation.key.qualifier_map
    departure = qualifiers.get("departure_at")
    if departure is None:
        if estimate.query_departure_at is not None:
            return None
    else:
        try:
            parsed = datetime.fromisoformat(
                departure.replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if (
            estimate.query_departure_at is None
            or _utc(
                estimate.query_departure_at,
                "TravelEstimate.query_departure_at",
            )
            != _utc(parsed, "route departure qualifier")
        ):
            return None
    return observation


def _joint_score(
    comparisons: tuple[LodgingComparisonCandidate, ...],
    schedule: ScheduleScore,
    route_durations: list[int],
    route_slot_count: int,
) -> LodgingItineraryScore:
    split_stay_count = _split_stay_count(comparisons)
    maximum_leg = max(route_durations, default=0)
    return LodgingItineraryScore(
        hard_violation_count=schedule.hard_violation_count,
        missing_required_count=schedule.missing_required_count,
        protected_change_count=schedule.protected_change_count,
        verification_risk_count=schedule.verification_risk_count,
        split_stay_count=split_stay_count,
        accepted_activity_change_count=(
            schedule.accepted_activity_change_count
        ),
        accepted_day_move_count=schedule.accepted_day_move_count,
        accepted_order_inversion_count=(
            schedule.accepted_order_inversion_count
        ),
        accepted_time_shift_deci_min=(
            schedule.accepted_time_shift_deci_min
        ),
        served_priority_points=schedule.served_priority_points,
        scheduled_optional_count=len(schedule.scheduled_optional_ids),
        soft_constraint_violation_count=(
            schedule.soft_constraint_violation_count
        ),
        tight_slack_count=schedule.tight_slack_count,
        slack_deficit_deci_min=schedule.slack_deficit_deci_min,
        activity_count_overage=schedule.activity_count_overage,
        service_overage_deci_min=schedule.service_overage_deci_min,
        max_lodging_leg_deci_min=maximum_leg,
        wait_deci_min=schedule.wait_deci_min,
        travel_deci_min=schedule.travel_deci_min,
        route_slot_count=route_slot_count,
    )


def _split_stay_count(
    comparisons: tuple[LodgingComparisonCandidate, ...],
) -> int:
    ordered = tuple(
        sorted(
            comparisons,
            key=lambda item: (
                item.candidate.check_in,
                item.candidate.check_out,
                item.candidate_id,
            ),
        )
    )
    endpoints = [
        item.identity.endpoint.location_id
        for item in ordered
        if item.identity.endpoint is not None
    ]
    return sum(
        left != right for left, right in zip(endpoints, endpoints[1:])
    )


def _option_issue(
    option: LodgingItineraryOption,
    code: str,
    severity: IssueSeverity,
    message: str,
    *,
    candidate_ids: tuple[str, ...] = (),
    slot_ids: tuple[str, ...] = (),
    actions: tuple[str, ...] = (),
) -> LodgingItineraryIssue:
    return LodgingItineraryIssue(
        code=code,
        severity=severity,
        message=message,
        option_ids=(option.option_id,),
        candidate_ids=candidate_ids,
        slot_ids=slot_ids,
        suggested_actions=actions,
    )


def _option_assessment(
    option: LodgingItineraryOption,
    disposition: LodgingOptionDisposition,
    issues: list[LodgingItineraryIssue],
) -> LodgingOptionAssessment:
    return LodgingOptionAssessment(
        option_id=option.option_id,
        disposition=disposition,
        candidate_ids=option.candidate_ids,
        issue_codes=tuple(item.code for item in issues),
        problem_id=option.problem.problem_id,
    )


__all__ = [
    "LODGING_ITINERARY_VERSION",
    "LodgingDayAnchor",
    "LodgingItineraryAssessment",
    "LodgingItineraryIssue",
    "LodgingItineraryOption",
    "LodgingItineraryScore",
    "LodgingItineraryStatus",
    "LodgingOptionAssessment",
    "LodgingOptionDisposition",
    "LodgingRouteUse",
    "assess_lodging_itineraries",
    "build_lodging_itinerary_problem",
]
