"""Deterministic timeline simulation and constraint checking.

This module is intentionally pure: it performs no I/O, provider calls, or
mutation.  Unknown evidence remains unknown instead of being converted to a
zero-duration shortcut.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    Activity,
    CheckIssue,
    CheckReport,
    CheckStatus,
    Constraint,
    ConstraintKind,
    ConstraintStrength,
    DayTimelineSummary,
    DaySpec,
    DecisionState,
    EvidenceState,
    Flexibility,
    IssueSeverity,
    TimelineEntry,
    TimeWindow,
    TravelEstimate,
    TripState,
)


IssueSink: TypeAlias = Callable[[CheckIssue], None]

_ACTIVE_DECISIONS = {
    DecisionState.SELECTED,
    DecisionState.FIXED,
    DecisionState.BOOKED,
}


@dataclass(frozen=True, slots=True)
class _FirstLeg:
    """Latest conflict-free placement of a day's first inbound travel."""

    day_id: str
    activity_id: str
    departure_at: datetime
    start_at: datetime
    verified: bool
    evidence_refs: tuple[str, ...]


def evaluate_timeline(
    state: TripState, *, now: datetime | None = None
) -> CheckReport:
    """Simulate ``state`` and return a structured, deterministic report.

    ``now`` is optional only to keep non-freshness fixtures ergonomic.  When a
    travel estimate has ``fresh_until`` but no evaluation time is supplied, the
    report explicitly becomes ``needs_verification`` instead of consulting the
    wall clock and making repeated runs nondeterministic.
    """

    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("evaluate_timeline(now=...) must be timezone-aware")

    issues: list[CheckIssue] = list(state.load_issues)
    seen_issues: set[tuple[object, ...]] = {
        _issue_identity(issue) for issue in state.load_issues
    }

    def add_issue(issue: CheckIssue) -> None:
        identity = _issue_identity(issue)
        if identity not in seen_issues:
            issues.append(issue)
            seen_issues.add(identity)

    timeline: list[TimelineEntry] = []
    day_summaries: list[DayTimelineSummary] = []
    timeline_verified: dict[str, bool] = {}
    day_completions: dict[str, tuple[datetime, bool]] = {}
    first_legs: dict[str, _FirstLeg] = {}
    used_travel: dict[str, TravelEstimate] = {}
    scheduled_ids: set[str] = set()
    metrics = {
        "activity_count": 0.0,
        "travel_min": 0.0,
        "buffer_min": 0.0,
        "wait_min": 0.0,
        "service_min": 0.0,
    }

    activity_by_id = state.activity_by_id
    hard_mode_limits = _hard_activity_mode_limits(state)
    referenced_ids = {
        activity_id for day in state.days for activity_id in day.activity_ids
    }

    for activity in state.activities:
        if _is_active(activity) and activity.activity_id not in referenced_ids:
            add_issue(
                _issue(
                    "UNSCHEDULED_SELECTED_ACTIVITY",
                    IssueSeverity.ERROR,
                    f"Selected activity {activity.activity_id!r} is not on any day.",
                    activity_ids=(activity.activity_id,),
                    fixes=("assign_activity_to_day", "change_decision_state"),
                )
            )

    for day in sorted(state.days, key=lambda item: (item.date, item.day_id)):
        try:
            zone = ZoneInfo(day.timezone or state.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            add_issue(
                _issue(
                    "INVALID_TIMEZONE",
                    IssueSeverity.ERROR,
                    f"Unknown IANA timezone {day.timezone or state.timezone!r}.",
                    details=(("day_id", day.day_id),),
                    fixes=("set_valid_iana_timezone",),
                )
            )
            continue

        day_activities = [
            activity_by_id[activity_id]
            for activity_id in day.activity_ids
            if _is_active(activity_by_id[activity_id])
        ]

        (
            entries,
            day_used_travel,
            day_metrics,
            day_verified,
            day_completion,
            first_leg,
            day_summary,
        ) = _simulate_day(
            day,
            day_activities,
            state.travel_estimates,
            hard_mode_limits,
            zone,
            now,
            add_issue,
        )
        timeline.extend(entries)
        day_summaries.append(day_summary)
        scheduled_ids.update(entry.activity_id for entry in entries)
        used_travel.update(day_used_travel)
        timeline_verified.update(day_verified)
        if day_completion is not None:
            day_completions[day.day_id] = day_completion
        if first_leg is not None:
            first_legs[day.day_id] = first_leg
        for key, value in day_metrics.items():
            metrics[key] += value

    _check_global_overlaps(
        tuple(timeline),
        timeline_verified,
        day_completions,
        first_legs,
        add_issue,
    )
    _check_constraints(
        state,
        tuple(timeline),
        frozenset(scheduled_ids),
        used_travel,
        timeline_verified,
        now,
        add_issue,
    )

    status = _derive_status(issues)
    metrics["verification_issue_count"] = float(
        sum(1 for issue in issues if _requires_verification(issue))
    )
    metrics["error_count"] = float(
        sum(1 for issue in issues if issue.severity is IssueSeverity.ERROR)
    )

    return CheckReport(
        status=status,
        issues=tuple(issues),
        timeline=tuple(timeline),
        metrics=tuple(metrics.items()),
        day_summaries=tuple(day_summaries),
    )


def _simulate_day(
    day: DaySpec,
    activities: list[Activity],
    travel_estimates: tuple[TravelEstimate, ...],
    hard_mode_limits: dict[str, frozenset[str]],
    zone: ZoneInfo,
    now: datetime | None,
    add_issue: IssueSink,
) -> tuple[
    list[TimelineEntry],
    dict[str, TravelEstimate],
    dict[str, float],
    dict[str, bool],
    tuple[datetime, bool] | None,
    _FirstLeg | None,
    DayTimelineSummary,
]:
    entries: list[TimelineEntry] = []
    used_travel: dict[str, TravelEstimate] = {}
    entry_verified: dict[str, bool] = {}
    first_leg: _FirstLeg | None = None
    metrics = {
        "activity_count": 0.0,
        "travel_min": 0.0,
        "buffer_min": 0.0,
        "wait_min": 0.0,
        "service_min": 0.0,
    }

    if not activities:
        available_end_at = (
            _clock_on_planning_day(
                day.date,
                day.available_end,
                day.available_start,
                day.available_end,
                zone,
                day.day_id,
                (),
                add_issue,
            )
            if day.available_end is not None
            else None
        )
        return (
            entries,
            used_travel,
            metrics,
            entry_verified,
            None,
            None,
            DayTimelineSummary(
                day_id=day.day_id,
                available_end_at=available_end_at,
                timing_verified=(
                    day.available_start is not None
                    and day.available_end is not None
                ),
            ),
        )

    if day.available_start is None or day.available_end is None:
        missing = []
        if day.available_start is None:
            missing.append("available_start")
        if day.available_end is None:
            missing.append("available_end")
        add_issue(
            _issue(
                "MISSING_DAY_BOUNDS",
                IssueSeverity.WARNING,
                f"Day {day.day_id!r} is missing {', '.join(missing)}.",
                details=(("day_id", day.day_id), ("missing", ",".join(missing))),
                fixes=("set_day_availability",),
            )
        )

    day_start_clock = day.available_start or time(0, 0)
    current_at = _localize(
        day.date,
        day_start_clock,
        zone,
        day.day_id,
        (),
        add_issue,
    )
    day_start = current_at if day.available_start is not None else None
    day_end = (
        _clock_on_planning_day(
            day.date,
            day.available_end,
            day.available_start,
            day.available_end,
            zone,
            day.day_id,
            (),
            add_issue,
        )
        if day.available_end is not None
        else None
    )
    current_location = day.start_location_id
    current_activity_id: str | None = None
    last_scheduled_at: datetime | None = None
    clock_verified = (
        day.available_start is not None and day.start_location_id is not None
    )
    if current_location is None:
        add_issue(
            _issue(
                "MISSING_START_LOCATION",
                IssueSeverity.WARNING,
                f"Day {day.day_id!r} has no start location.",
                details=(("day_id", day.day_id),),
                fixes=("set_day_start_location",),
            )
        )

    for activity_index, activity in enumerate(activities):
        travel_duration = 0.0
        buffer_duration = 0.0
        travel_verified = True
        inbound_estimate: TravelEstimate | None = None
        if current_location is not None and current_location != activity.location_id:
            estimate = _choose_travel(
                travel_estimates,
                day,
                current_location,
                activity.location_id,
                current_activity_id,
                activity.activity_id,
                _activity_allowed_modes(
                    day, activity.activity_id, hard_mode_limits
                ),
                route_query_at=current_at,
                now=now,
            )
            if estimate is None:
                travel_verified = False
                add_issue(
                    _missing_travel_issue(
                        day.day_id,
                        current_location,
                        activity.location_id,
                        activity.activity_id,
                    )
                )
            else:
                inbound_estimate = estimate
                used_travel[activity.activity_id] = estimate
                travel_duration = float(estimate.duration_min)
                buffer_duration = float(estimate.buffer_min)
                _check_travel_evidence(estimate, now, activity.activity_id, add_issue)
                travel_verified = _travel_is_verified(estimate, now)
                if day.allowed_modes and estimate.mode not in day.allowed_modes:
                    add_issue(
                        _issue(
                            "DISALLOWED_MODE",
                            IssueSeverity.ERROR,
                            (
                                f"Travel to {activity.activity_id!r} uses "
                                f"{estimate.mode!r}, which is not allowed on "
                                f"{day.day_id!r}."
                            ),
                            activity_ids=(activity.activity_id,),
                            evidence_refs=_evidence_refs(estimate),
                            details=(
                                ("day_id", day.day_id),
                                ("mode", estimate.mode),
                            ),
                            fixes=("choose_allowed_travel_mode",),
                        )
                    )
        elif current_location is None:
            travel_verified = False
            add_issue(
                _missing_travel_issue(
                    day.day_id,
                    "unknown-day-start",
                    activity.location_id,
                    activity.activity_id,
                )
            )

        arrival_at = _add_elapsed(
            current_at, travel_duration + buffer_duration
        )
        arrival_verified = (
            clock_verified
            and travel_verified
            and activity.evidence_state is EvidenceState.VERIFIED
        )
        metrics["travel_min"] += travel_duration
        metrics["buffer_min"] += buffer_duration

        duration = activity.duration_min
        if duration is None:
            add_issue(
                _issue(
                    "MISSING_DURATION",
                    IssueSeverity.WARNING,
                    f"Activity {activity.activity_id!r} has no duration.",
                    activity_ids=(activity.activity_id,),
                    fixes=("set_activity_duration",),
                )
            )
            simulated_duration = 0.0
        else:
            simulated_duration = float(duration)
        duration_verified = (
            duration is not None
            and activity.evidence_state is EvidenceState.VERIFIED
        )

        scheduled_at = (
            _clock_on_planning_day(
                day.date,
                activity.scheduled_start,
                day.available_start,
                day.available_end,
                zone,
                day.day_id,
                (activity.activity_id,),
                add_issue,
            )
            if activity.scheduled_start is not None
            else None
        )
        if (
            scheduled_at is not None
            and last_scheduled_at is not None
            and (day.available_start is None or day.available_end is None)
            and _after(last_scheduled_at, scheduled_at)
        ):
            original_date = scheduled_at.date()
            while _after(last_scheduled_at, scheduled_at):
                scheduled_at = _localize(
                    scheduled_at.date() + timedelta(days=1),
                    activity.scheduled_start,
                    zone,
                    day.day_id,
                    (activity.activity_id,),
                    add_issue,
                )
            add_issue(
                _issue(
                    "SCHEDULE_DATE_ROLLOVER_INFERRED",
                    IssueSeverity.WARNING,
                    (
                        f"Activity {activity.activity_id!r} was placed on "
                        f"{scheduled_at.date().isoformat()} to preserve order "
                        "because the day has incomplete bounds."
                    ),
                    activity_ids=(activity.activity_id,),
                    details=(
                        ("day_id", day.day_id),
                        ("from_date", original_date.isoformat()),
                        ("to_date", scheduled_at.date().isoformat()),
                    ),
                    fixes=("set_day_availability", "set_explicit_activity_datetime"),
                )
            )
        if scheduled_at is not None:
            last_scheduled_at = scheduled_at

        if scheduled_at is not None and _after(arrival_at, scheduled_at):
            fixed = activity.flexibility is Flexibility.FIXED_TIME
            code = (
                "FIXED_TIME_CONFLICT" if fixed else "SCHEDULED_START_CONFLICT"
            )
            if not arrival_verified:
                code = f"POSSIBLE_{code}"
            add_issue(
                _issue(
                    code,
                    (
                        IssueSeverity.ERROR
                        if arrival_verified
                        else IssueSeverity.WARNING
                    ),
                    (
                        f"Activity {activity.activity_id!r} cannot start at "
                        f"{activity.scheduled_start.isoformat(timespec='minutes')}; "
                        f"earliest arrival is {arrival_at.isoformat()}."
                    ),
                    activity_ids=(activity.activity_id,),
                    details=(("day_id", day.day_id),),
                    fixes=("move_activity", "change_previous_activity", "change_route"),
                )
            )

        proposed_start = _latest(
            value for value in (arrival_at, scheduled_at) if value is not None
        )
        start_at, window_end = _fit_activity_window(
            day,
            activity,
            proposed_start,
            simulated_duration,
            zone,
            arrival_verified and duration_verified,
            add_issue,
        )
        end_at = _add_elapsed(start_at, simulated_duration)
        if (
            activity_index == 0
            and inbound_estimate is not None
            and travel_duration + buffer_duration > 0
        ):
            first_leg = _FirstLeg(
                day_id=day.day_id,
                activity_id=activity.activity_id,
                departure_at=_add_elapsed(
                    start_at, -(travel_duration + buffer_duration)
                ),
                start_at=start_at,
                verified=arrival_verified,
                evidence_refs=_evidence_refs(inbound_estimate),
            )
        wait_duration = max(
            0.0, _elapsed_minutes(arrival_at, start_at)
        )

        entry_is_verified = arrival_verified and duration_verified
        fixed_schedule_outside_day = (
            activity.flexibility is Flexibility.FIXED_TIME
            and scheduled_at is not None
            and (
                (day_start is not None and _after(day_start, scheduled_at))
                or (day_end is not None and _after(scheduled_at, day_end))
                or (
                    day_end is not None
                    and duration is not None
                    and _after(
                        _add_elapsed(scheduled_at, float(duration)),
                        day_end,
                    )
                )
            )
        )
        day_window_violated = (
            (day_end is not None and _after(end_at, day_end))
            or fixed_schedule_outside_day
        )
        if day_window_violated:
            violation_is_hard = (
                entry_is_verified or fixed_schedule_outside_day
            )
            code = (
                "DAY_WINDOW_VIOLATION"
                if violation_is_hard
                else "POSSIBLE_DAY_WINDOW_VIOLATION"
            )
            add_issue(
                _issue(
                    code,
                    (
                        IssueSeverity.ERROR
                        if violation_is_hard
                        else IssueSeverity.WARNING
                    ),
                    (
                        f"Activity {activity.activity_id!r} falls outside "
                        f"{day.day_id!r} availability."
                    ),
                    activity_ids=(activity.activity_id,),
                    details=(("day_id", day.day_id),),
                    fixes=("move_activity", "shorten_activity", "extend_day_availability"),
                )
            )

        _check_activity_evidence(activity, add_issue)

        slack = None
        applicable_end = window_end
        if day_end is not None:
            applicable_end = (
                _earliest((window_end, day_end))
                if window_end is not None
                else day_end
            )
        if applicable_end is not None:
            slack = _elapsed_minutes(end_at, applicable_end)

        entries.append(
            TimelineEntry(
                activity_id=activity.activity_id,
                day_id=day.day_id,
                location_id=activity.location_id,
                arrival_at=arrival_at,
                start_at=start_at,
                end_at=end_at,
                travel_duration_min=travel_duration,
                wait_duration_min=wait_duration,
                slack_min=slack,
            )
        )
        entry_verified[activity.activity_id] = entry_is_verified
        metrics["activity_count"] += 1.0
        metrics["wait_min"] += wait_duration
        metrics["service_min"] += simulated_duration
        current_at = end_at
        current_location = activity.location_id
        current_activity_id = activity.activity_id
        clock_verified = entry_is_verified

    completion_at = current_at
    completion_verified = clock_verified
    if day.end_location_id is None:
        completion_verified = False
        add_issue(
            _issue(
                "MISSING_END_LOCATION",
                IssueSeverity.WARNING,
                f"Day {day.day_id!r} has no end location.",
                details=(("day_id", day.day_id),),
                fixes=("set_day_end_location",),
            )
        )
    elif current_location != day.end_location_id:
        estimate = _choose_travel(
            travel_estimates,
            day,
            current_location,
            day.end_location_id,
            current_activity_id,
            None,
            frozenset(day.allowed_modes) if day.allowed_modes else None,
            route_query_at=current_at,
            now=now,
        )
        if estimate is None:
            completion_verified = False
            add_issue(
                _missing_travel_issue(
                    day.day_id,
                    current_location or "unknown",
                    day.end_location_id,
                    None,
                )
            )
        else:
            return_key = f"{day.day_id}:return"
            used_travel[return_key] = estimate
            _check_travel_evidence(estimate, now, None, add_issue)
            return_verified = (
                clock_verified and _travel_is_verified(estimate, now)
            )
            if day.allowed_modes and estimate.mode not in day.allowed_modes:
                add_issue(
                    _issue(
                        "DISALLOWED_MODE",
                        IssueSeverity.ERROR,
                        (
                            f"Return travel on {day.day_id!r} uses "
                            f"{estimate.mode!r}, which is not allowed."
                        ),
                        evidence_refs=_evidence_refs(estimate),
                        details=(("day_id", day.day_id), ("mode", estimate.mode)),
                        fixes=("choose_allowed_travel_mode",),
                    )
                )
            return_at = _add_elapsed(
                current_at,
                float(estimate.duration_min) + float(estimate.buffer_min),
            )
            completion_at = return_at
            completion_verified = return_verified
            metrics["travel_min"] += float(estimate.duration_min)
            metrics["buffer_min"] += float(estimate.buffer_min)
            if day_end is not None and _after(return_at, day_end):
                code = (
                    "RETURN_AFTER_DAY_END"
                    if return_verified
                    else "POSSIBLE_RETURN_AFTER_DAY_END"
                )
                add_issue(
                    _issue(
                        code,
                        (
                            IssueSeverity.ERROR
                            if return_verified
                            else IssueSeverity.WARNING
                        ),
                        f"Return to base on {day.day_id!r} is after day availability.",
                        details=(("day_id", day.day_id),),
                        evidence_refs=_evidence_refs(estimate),
                        fixes=("move_last_activity", "change_route", "extend_day_availability"),
                    )
                )

    summary_start = (
        first_leg.departure_at
        if first_leg is not None
        else entries[0].arrival_at
    )
    end_slack_min = (
        _elapsed_minutes(completion_at, day_end)
        if completion_verified and day_end is not None
        else None
    )
    summary = DayTimelineSummary(
        day_id=day.day_id,
        starts_at=summary_start,
        completes_at=completion_at,
        available_end_at=day_end,
        end_slack_min=end_slack_min,
        activity_count=int(metrics["activity_count"]),
        service_min=metrics["service_min"],
        travel_min=metrics["travel_min"],
        buffer_min=metrics["buffer_min"],
        wait_min=metrics["wait_min"],
        timing_verified=completion_verified,
    )

    return (
        entries,
        used_travel,
        metrics,
        entry_verified,
        (completion_at, completion_verified),
        first_leg,
        summary,
    )


def _fit_activity_window(
    day: DaySpec,
    activity: Activity,
    proposed_start: datetime,
    duration_min: float,
    zone: ZoneInfo,
    timing_verified: bool,
    add_issue: IssueSink,
) -> tuple[datetime, datetime | None]:
    if not activity.allowed_windows:
        return proposed_start, None

    windows = [
        _window_on_planning_day(
            day,
            window,
            zone,
            (activity.activity_id,),
            add_issue,
        )
        for window in activity.allowed_windows
    ]
    windows.sort(key=lambda pair: (_instant(pair[0]), _instant(pair[1])))

    for window_start, window_end in windows:
        candidate_start = _latest((proposed_start, window_start))
        candidate_end = _add_elapsed(candidate_start, duration_min)
        if not _after(candidate_end, window_end):
            return candidate_start, window_end

    code = (
        "TIME_WINDOW_VIOLATION"
        if timing_verified
        else "POSSIBLE_TIME_WINDOW_VIOLATION"
    )
    add_issue(
        _issue(
            code,
            IssueSeverity.ERROR if timing_verified else IssueSeverity.WARNING,
            (
                f"Activity {activity.activity_id!r} cannot fit its full "
                "duration inside an allowed window."
            ),
            activity_ids=(activity.activity_id,),
            details=(("day_id", day.day_id),),
            fixes=("move_activity", "shorten_activity", "refresh_opening_hours"),
        )
    )
    return proposed_start, None


def _hard_activity_mode_limits(
    state: TripState,
) -> dict[str, frozenset[str]]:
    limits: dict[str, frozenset[str]] = {}
    activity_ids = frozenset(state.activity_by_id)
    for constraint in state.constraints:
        if (
            constraint.kind is not ConstraintKind.ALLOWED_MODE
            or constraint.strength is not ConstraintStrength.HARD
        ):
            continue
        allowed = _csv_param(
            constraint.param("modes", constraint.param("mode"))
        )
        if not allowed:
            continue
        for subject_id in constraint.subject_ids:
            if subject_id not in activity_ids:
                continue
            limits[subject_id] = (
                limits[subject_id].intersection(allowed)
                if subject_id in limits
                else allowed
            )
    return limits


def _activity_allowed_modes(
    day: DaySpec,
    activity_id: str,
    hard_mode_limits: dict[str, frozenset[str]],
) -> frozenset[str] | None:
    day_modes = frozenset(day.allowed_modes) if day.allowed_modes else None
    activity_modes = hard_mode_limits.get(activity_id)
    if day_modes is None:
        return activity_modes
    if activity_modes is None:
        return day_modes
    return day_modes.intersection(activity_modes)


def _choose_travel(
    estimates: Iterable[TravelEstimate],
    day: DaySpec,
    from_location_id: str | None,
    to_location_id: str,
    from_activity_id: str | None,
    to_activity_id: str | None,
    allowed_modes: frozenset[str] | None,
    *,
    route_query_at: datetime,
    now: datetime | None,
) -> TravelEstimate | None:
    if from_location_id is None or from_location_id == to_location_id:
        return None

    candidates = [
        estimate
        for estimate in estimates
        if estimate.from_location_id == from_location_id
        and estimate.to_location_id == to_location_id
        and estimate.day_id in (None, day.day_id)
        and estimate.from_activity_id in (None, from_activity_id)
        and estimate.to_activity_id in (None, to_activity_id)
        and _route_matches_query_context(estimate, route_query_at)
    ]
    if not candidates:
        return None
    if allowed_modes is not None:
        allowed_candidates = [
            estimate
            for estimate in candidates
            if estimate.mode in allowed_modes
        ]
        if allowed_candidates:
            candidates = allowed_candidates

    timed_candidates = [
        estimate
        for estimate in candidates
        if estimate.query_departure_at is not None
        or estimate.query_arrival_at is not None
    ]
    if timed_candidates:
        candidates = timed_candidates

    evidence_rank = {
        EvidenceState.VERIFIED: 0,
        EvidenceState.UNVERIFIED: 1,
        EvidenceState.STALE: 2,
        EvidenceState.CONFLICTED: 3,
    }
    candidates.sort(
        key=lambda estimate: (
            0 if _travel_is_verified(estimate, now) else 1,
            -sum(
                (
                    estimate.from_activity_id == from_activity_id
                    and from_activity_id is not None,
                    estimate.to_activity_id == to_activity_id
                    and to_activity_id is not None,
                )
            ),
            0 if estimate.day_id == day.day_id else 1,
            evidence_rank[estimate.evidence_state],
            0 if estimate.recommended else 1,
            float(estimate.duration_min) + float(estimate.buffer_min),
            estimate.mode,
            estimate.evidence_ref or "",
        )
    )
    return candidates[0]


def _route_matches_query_context(
    estimate: TravelEstimate,
    route_query_at: datetime | None,
) -> bool:
    if estimate.query_departure_at is not None:
        return (
            route_query_at is not None
            and _instant(estimate.query_departure_at)
            == _instant(route_query_at)
        )
    if estimate.query_arrival_at is not None:
        return (
            route_query_at is not None
            and _instant(estimate.query_arrival_at)
            == _instant(
                _add_elapsed(route_query_at, float(estimate.duration_min))
            )
        )
    return True


def _check_travel_evidence(
    estimate: TravelEstimate,
    now: datetime | None,
    activity_id: str | None,
    add_issue: IssueSink,
) -> None:
    activity_ids = (activity_id,) if activity_id is not None else ()
    refs = _evidence_refs(estimate)
    disclosure_details = _travel_details(estimate) + (
        ("status_effect", "none"),
    )

    for warning_code in estimate.warning_codes:
        add_issue(
            _issue(
                "ROUTE_PROVIDER_WARNING",
                IssueSeverity.WARNING,
                (
                    f"Route provider warning {warning_code!r} applies to the "
                    f"selected {estimate.mode!r} estimate."
                ),
                activity_ids=activity_ids,
                evidence_refs=refs,
                details=disclosure_details
                + (("warning_code", warning_code),),
            )
        )
    if estimate.fallback_from_mode is not None:
        add_issue(
            _issue(
                "TRANSIT_FALLBACK_DISCLOSURE",
                IssueSeverity.WARNING,
                (
                    f"Route mode {estimate.fallback_from_mode!r} fell back to "
                    f"{estimate.mode!r}; the estimate remains "
                    f"{estimate.mode!r}."
                ),
                activity_ids=activity_ids,
                evidence_refs=refs,
                details=disclosure_details
                + (
                    (
                        "fallback_from_mode",
                        estimate.fallback_from_mode,
                    ),
                ),
            )
        )

    if estimate.evidence_state is EvidenceState.UNVERIFIED:
        add_issue(
            _issue(
                "UNVERIFIED_EVIDENCE",
                IssueSeverity.WARNING,
                "A travel duration has not been verified.",
                activity_ids=activity_ids,
                evidence_refs=refs,
                details=_travel_details(estimate),
                fixes=("verify_travel_estimate",),
            )
        )
    elif estimate.evidence_state is EvidenceState.STALE:
        add_issue(
            _issue(
                "STALE_EVIDENCE",
                IssueSeverity.WARNING,
                "A travel duration is marked stale.",
                activity_ids=activity_ids,
                evidence_refs=refs,
                details=_travel_details(estimate),
                fixes=("refresh_travel_estimate",),
            )
        )
    elif estimate.evidence_state is EvidenceState.CONFLICTED:
        add_issue(
            _issue(
                "CONFLICTED_EVIDENCE",
                IssueSeverity.WARNING,
                "Travel providers disagree about a duration.",
                activity_ids=activity_ids,
                evidence_refs=refs,
                details=_travel_details(estimate),
                fixes=("resolve_travel_evidence",),
            )
        )

    if estimate.fresh_until is None:
        return
    if (
        estimate.fresh_until.tzinfo is None
        or estimate.fresh_until.utcoffset() is None
    ):
        add_issue(
            _issue(
                "INVALID_FRESHNESS_TIMESTAMP",
                IssueSeverity.WARNING,
                "Travel freshness timestamp has no timezone.",
                activity_ids=activity_ids,
                evidence_refs=refs,
                details=_travel_details(estimate),
                fixes=("refresh_travel_estimate",),
            )
        )
    elif now is None:
        add_issue(
            _issue(
                "FRESHNESS_NOT_EVALUATED",
                IssueSeverity.WARNING,
                "An evaluation time is required to check travel freshness.",
                activity_ids=activity_ids,
                evidence_refs=refs,
                details=_travel_details(estimate),
                fixes=("evaluate_with_as_of_time",),
            )
        )
    elif estimate.fresh_until.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
        add_issue(
            _issue(
                "STALE_EVIDENCE",
                IssueSeverity.WARNING,
                "A travel duration is past its freshness horizon.",
                activity_ids=activity_ids,
                evidence_refs=refs,
                details=_travel_details(estimate),
                fixes=("refresh_travel_estimate",),
            )
        )


def _travel_is_verified(
    estimate: TravelEstimate, now: datetime | None
) -> bool:
    if estimate.evidence_state is not EvidenceState.VERIFIED:
        return False
    if estimate.fresh_until is None:
        return True
    if (
        estimate.fresh_until.tzinfo is None
        or estimate.fresh_until.utcoffset() is None
        or now is None
    ):
        return False
    return (
        estimate.fresh_until.astimezone(timezone.utc)
        > now.astimezone(timezone.utc)
    )


def _check_activity_evidence(activity: Activity, add_issue: IssueSink) -> None:
    if activity.evidence_state is EvidenceState.VERIFIED:
        return
    code = {
        EvidenceState.UNVERIFIED: "UNVERIFIED_EVIDENCE",
        EvidenceState.STALE: "STALE_EVIDENCE",
        EvidenceState.CONFLICTED: "CONFLICTED_EVIDENCE",
    }[activity.evidence_state]
    add_issue(
        _issue(
            code,
            IssueSeverity.WARNING,
            f"Activity {activity.activity_id!r} evidence is {activity.evidence_state.value}.",
            activity_ids=(activity.activity_id,),
            evidence_refs=(f"activity:{activity.activity_id}",),
            fixes=("verify_activity_fact",),
        )
    )


def _check_global_overlaps(
    timeline: tuple[TimelineEntry, ...],
    timeline_verified: dict[str, bool],
    day_completions: dict[str, tuple[datetime, bool]],
    first_legs: dict[str, _FirstLeg],
    add_issue: IssueSink,
) -> None:
    ordered = sorted(
        timeline,
        key=lambda entry: (
            _instant(entry.start_at),
            _instant(entry.end_at),
            entry.activity_id,
        ),
    )
    active: TimelineEntry | None = None
    for entry in ordered:
        if active is not None and _after(active.end_at, entry.start_at):
            verified = timeline_verified.get(
                active.activity_id, False
            ) and timeline_verified.get(entry.activity_id, False)
            add_issue(
                _issue(
                    (
                        "GLOBAL_TIMELINE_OVERLAP"
                        if verified
                        else "POSSIBLE_GLOBAL_TIMELINE_OVERLAP"
                    ),
                    IssueSeverity.ERROR if verified else IssueSeverity.WARNING,
                    (
                        f"Activities {active.activity_id!r} and "
                        f"{entry.activity_id!r} overlap in absolute time."
                    ),
                    activity_ids=(active.activity_id, entry.activity_id),
                    details=(
                        ("first_day_id", active.day_id),
                        ("second_day_id", entry.day_id),
                    ),
                    fixes=("move_activity", "shorten_activity"),
                )
            )
        if active is None or _after(entry.end_at, active.end_at):
            active = entry

    entries_by_day: dict[str, list[TimelineEntry]] = {}
    for entry in ordered:
        entries_by_day.setdefault(entry.day_id, []).append(entry)
    for day_id, (completion_at, completion_verified) in day_completions.items():
        day_entries = entries_by_day.get(day_id, [])
        if not day_entries:
            continue
        last_end = _latest(entry.end_at for entry in day_entries)
        if not _after(completion_at, last_end):
            continue
        for entry in ordered:
            if entry.day_id == day_id:
                continue
            if _instant(entry.start_at) < _instant(last_end):
                continue
            if not _after(completion_at, entry.start_at):
                break
            verified = completion_verified and timeline_verified.get(
                entry.activity_id, False
            )
            add_issue(
                _issue(
                    (
                        "RETURN_TIMELINE_OVERLAP"
                        if verified
                        else "POSSIBLE_RETURN_TIMELINE_OVERLAP"
                    ),
                    IssueSeverity.ERROR if verified else IssueSeverity.WARNING,
                    (
                        f"Return travel after {day_id!r} overlaps activity "
                        f"{entry.activity_id!r}."
                    ),
                    activity_ids=(entry.activity_id,),
                    details=(
                        ("return_day_id", day_id),
                        ("activity_day_id", entry.day_id),
                    ),
                    fixes=("move_activity", "change_return_route"),
                )
            )
            break

    for leg in sorted(
        first_legs.values(),
        key=lambda item: (
            _instant(item.departure_at),
            _instant(item.start_at),
            item.day_id,
        ),
    ):
        for previous_day_id, (
            completion_at,
            completion_verified,
        ) in day_completions.items():
            if previous_day_id == leg.day_id:
                continue
            if not _after(completion_at, leg.departure_at):
                continue
            if _after(completion_at, leg.start_at):
                # Activity/return overlap checks above already cover this span.
                continue
            verified = completion_verified and leg.verified
            add_issue(
                _issue(
                    (
                        "INBOUND_TIMELINE_OVERLAP"
                        if verified
                        else "POSSIBLE_INBOUND_TIMELINE_OVERLAP"
                    ),
                    IssueSeverity.ERROR if verified else IssueSeverity.WARNING,
                    (
                        f"Completion of {previous_day_id!r} overlaps inbound "
                        f"travel to activity {leg.activity_id!r}."
                    ),
                    activity_ids=(leg.activity_id,),
                    evidence_refs=leg.evidence_refs,
                    details=(
                        ("previous_day_id", previous_day_id),
                        ("inbound_day_id", leg.day_id),
                        ("latest_departure_at", leg.departure_at.isoformat()),
                    ),
                    fixes=("move_activity", "change_inbound_route"),
                )
            )


def _check_constraints(
    state: TripState,
    timeline: tuple[TimelineEntry, ...],
    scheduled_ids: frozenset[str],
    used_travel: dict[str, TravelEstimate],
    timeline_verified: dict[str, bool],
    now: datetime | None,
    add_issue: IssueSink,
) -> None:
    entry_by_id = {entry.activity_id: entry for entry in timeline}
    activity_by_id = state.activity_by_id
    day_by_id = state.day_by_id
    activity_subject_kinds = {
        ConstraintKind.MUST_INCLUDE,
        ConstraintKind.EXACTLY_ONCE,
        ConstraintKind.AT_MOST_ONCE,
        ConstraintKind.BEFORE,
        ConstraintKind.REQUIRES,
        ConstraintKind.CHOOSE_N,
        ConstraintKind.ALLOWED_DAY,
        ConstraintKind.FIXED_TIME,
        ConstraintKind.ALLOWED_WINDOW,
        ConstraintKind.ALLOWED_MODE,
    }

    for constraint in state.constraints:
        kind = constraint.kind
        if kind is ConstraintKind.MUST_INCLUDE and not constraint.subject_ids:
            _invalid_constraint(
                constraint, "MUST_INCLUDE needs at least one subject.", add_issue
            )
            continue
        if kind in activity_subject_kinds:
            if not constraint.subject_ids:
                _invalid_constraint(
                    constraint, f"{kind.value} needs activity subjects.", add_issue
                )
                continue
            unknown_subjects = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id not in activity_by_id
            )
            if unknown_subjects:
                _invalid_constraint(
                    constraint,
                    (
                        f"{kind.value} refers to non-activity subjects: "
                        f"{', '.join(unknown_subjects)}."
                    ),
                    add_issue,
                )
                continue
        count = sum(subject_id in scheduled_ids for subject_id in constraint.subject_ids)

        if kind is ConstraintKind.MUST_INCLUDE:
            missing = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id not in scheduled_ids
            )
            if missing:
                _constraint_issue(
                    constraint,
                    "MISSING_REQUIRED_ACTIVITY",
                    f"Required activities are not scheduled: {', '.join(missing)}.",
                    missing,
                    add_issue,
                    ("schedule_required_activity",),
                )
        elif kind is ConstraintKind.EXACTLY_ONCE:
            if count != 1:
                _constraint_issue(
                    constraint,
                    "EXACTLY_ONCE_VIOLATION",
                    f"Expected exactly one active variant, found {count}.",
                    constraint.subject_ids,
                    add_issue,
                    ("select_exactly_one_variant",),
                )
        elif kind is ConstraintKind.AT_MOST_ONCE:
            if count > 1:
                _constraint_issue(
                    constraint,
                    "AT_MOST_ONCE_VIOLATION",
                    f"Expected at most one active variant, found {count}.",
                    constraint.subject_ids,
                    add_issue,
                    ("keep_one_variant",),
                )
        elif kind is ConstraintKind.BEFORE:
            if len(constraint.subject_ids) != 2:
                _invalid_constraint(constraint, "BEFORE needs two subjects.", add_issue)
                continue
            before_id, after_id = constraint.subject_ids
            if before_id not in entry_by_id or after_id not in entry_by_id:
                _constraint_issue(
                    constraint,
                    "PRECEDENCE_SUBJECT_MISSING",
                    "A precedence subject is not scheduled.",
                    constraint.subject_ids,
                    add_issue,
                    ("schedule_constraint_subjects",),
                )
            elif _after(
                entry_by_id[before_id].end_at, entry_by_id[after_id].start_at
            ):
                if timeline_verified.get(before_id) and timeline_verified.get(after_id):
                    _constraint_issue(
                        constraint,
                        "PRECEDENCE_VIOLATION",
                        f"{before_id!r} does not finish before {after_id!r}.",
                        constraint.subject_ids,
                        add_issue,
                        ("reorder_activities",),
                    )
                else:
                    _possible_constraint_issue(
                        constraint,
                        "POSSIBLE_PRECEDENCE_VIOLATION",
                        (
                            f"Unverified timing may prevent {before_id!r} from "
                            f"finishing before {after_id!r}."
                        ),
                        constraint.subject_ids,
                        add_issue,
                        ("verify_timing_evidence", "reorder_activities"),
                    )
        elif kind is ConstraintKind.REQUIRES:
            if len(constraint.subject_ids) < 2:
                _invalid_constraint(
                    constraint, "REQUIRES needs a trigger and dependency.", add_issue
                )
                continue
            trigger, *dependencies = constraint.subject_ids
            missing = tuple(
                dependency
                for dependency in dependencies
                if trigger in scheduled_ids and dependency not in scheduled_ids
            )
            if missing:
                _constraint_issue(
                    constraint,
                    "REQUIRES_VIOLATION",
                    f"{trigger!r} requires: {', '.join(missing)}.",
                    (trigger, *missing),
                    add_issue,
                    ("schedule_dependencies", "remove_trigger_activity"),
                )
        elif kind is ConstraintKind.CHOOSE_N:
            expected = constraint.param("n")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                _invalid_constraint(
                    constraint, "CHOOSE_N needs a non-negative integer n.", add_issue
                )
                continue
            if count != expected:
                _constraint_issue(
                    constraint,
                    "CHOOSE_N_VIOLATION",
                    f"Expected {expected} choices, found {count}.",
                    constraint.subject_ids,
                    add_issue,
                    ("change_selected_choices",),
                )
        elif kind is ConstraintKind.ALLOWED_DAY:
            allowed = _csv_param(
                constraint.param("day_ids", constraint.param("day_id"))
            )
            if not allowed:
                _invalid_constraint(
                    constraint, "ALLOWED_DAY needs day_id or day_ids.", add_issue
                )
                continue
            unknown_days = sorted(allowed.difference(day_by_id))
            if unknown_days:
                _invalid_constraint(
                    constraint,
                    f"ALLOWED_DAY refers to unknown days: {', '.join(unknown_days)}.",
                    add_issue,
                )
                continue
            wrong = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id in scheduled_ids
                and activity_by_id[subject_id].day_id not in allowed
            )
            if wrong:
                _constraint_issue(
                    constraint,
                    "ALLOWED_DAY_VIOLATION",
                    f"Activities are scheduled outside allowed days: {', '.join(wrong)}.",
                    wrong,
                    add_issue,
                    ("move_activity_to_allowed_day",),
                )
        elif kind is ConstraintKind.FIXED_TIME:
            expected_text = constraint.param("time", constraint.param("start"))
            expected = _parse_constraint_time(expected_text)
            if expected is None:
                _invalid_constraint(
                    constraint, "FIXED_TIME needs time='HH:MM'.", add_issue
                )
                continue
            structural_wrong = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id in scheduled_ids
                and activity_by_id[subject_id].scheduled_start != expected
            )
            runtime_wrong = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id in scheduled_ids
                and subject_id not in structural_wrong
                and entry_by_id[subject_id].start_at.timetz().replace(tzinfo=None)
                != expected
            )
            verified_runtime_wrong = tuple(
                subject_id
                for subject_id in runtime_wrong
                if timeline_verified.get(subject_id, False)
            )
            uncertain_runtime_wrong = tuple(
                subject_id
                for subject_id in runtime_wrong
                if not timeline_verified.get(subject_id, False)
            )
            if structural_wrong:
                _constraint_issue(
                    constraint,
                    "FIXED_TIME_CONFLICT",
                    (
                        "Activity scheduled_start contradicts fixed time "
                        f"{expected.isoformat(timespec='minutes')}."
                    ),
                    structural_wrong,
                    add_issue,
                    ("restore_fixed_time",),
                )
            if verified_runtime_wrong:
                _constraint_issue(
                    constraint,
                    "FIXED_TIME_CONFLICT",
                    f"Activities miss fixed time {expected.isoformat(timespec='minutes')}.",
                    verified_runtime_wrong,
                    add_issue,
                    ("restore_fixed_time",),
                )
            if uncertain_runtime_wrong:
                _possible_constraint_issue(
                    constraint,
                    "POSSIBLE_FIXED_TIME_CONFLICT",
                    (
                        "Unverified timing may miss fixed time "
                        f"{expected.isoformat(timespec='minutes')}."
                    ),
                    uncertain_runtime_wrong,
                    add_issue,
                    ("verify_timing_evidence", "restore_fixed_time"),
                )
        elif kind is ConstraintKind.ALLOWED_WINDOW:
            start = _parse_constraint_time(constraint.param("start"))
            end = _parse_constraint_time(constraint.param("end"))
            if start is None or end is None:
                _invalid_constraint(
                    constraint, "ALLOWED_WINDOW needs start and end.", add_issue
                )
                continue
            wrong = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id in entry_by_id
                and not _entry_inside_clock_window(
                    entry_by_id[subject_id], start, end, add_issue
                )
            )
            verified_wrong = tuple(
                subject_id
                for subject_id in wrong
                if timeline_verified.get(subject_id, False)
            )
            uncertain_wrong = tuple(
                subject_id
                for subject_id in wrong
                if not timeline_verified.get(subject_id, False)
            )
            if verified_wrong:
                _constraint_issue(
                    constraint,
                    "TIME_WINDOW_VIOLATION",
                    "Activities do not fit the allowed time window.",
                    verified_wrong,
                    add_issue,
                    ("move_activity", "shorten_activity"),
                )
            if uncertain_wrong:
                _possible_constraint_issue(
                    constraint,
                    "POSSIBLE_TIME_WINDOW_VIOLATION",
                    "Unverified timing may fall outside the allowed time window.",
                    uncertain_wrong,
                    add_issue,
                    ("verify_timing_evidence", "move_activity"),
                )
        elif kind is ConstraintKind.ALLOWED_MODE:
            allowed = _csv_param(
                constraint.param("modes", constraint.param("mode"))
            )
            if not allowed:
                _invalid_constraint(
                    constraint, "ALLOWED_MODE needs mode or modes.", add_issue
                )
                continue
            wrong = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id in scheduled_ids
                and subject_id in used_travel
                and used_travel[subject_id].mode not in allowed
            )
            if wrong:
                _constraint_issue(
                    constraint,
                    "DISALLOWED_MODE",
                    "Travel uses a mode outside the allowed set.",
                    wrong,
                    add_issue,
                    ("choose_allowed_travel_mode",),
                )
        elif kind is ConstraintKind.DAILY_LIMIT:
            max_activities = constraint.param("max_activities")
            max_minutes = constraint.param("max_minutes")
            if max_activities is None and max_minutes is None:
                _invalid_constraint(
                    constraint,
                    "DAILY_LIMIT needs max_activities or max_minutes.",
                    add_issue,
                )
                continue
            if max_activities is not None and (
                isinstance(max_activities, bool)
                or not isinstance(max_activities, int)
                or max_activities < 0
            ):
                _invalid_constraint(
                    constraint,
                    "DAILY_LIMIT max_activities must be a non-negative integer.",
                    add_issue,
                )
                continue
            if max_minutes is not None and (
                isinstance(max_minutes, bool)
                or not isinstance(max_minutes, (int, float))
                or float(max_minutes) < 0
            ):
                _invalid_constraint(
                    constraint,
                    "DAILY_LIMIT max_minutes must be a non-negative number.",
                    add_issue,
                )
                continue
            unknown_days = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id not in day_by_id
            )
            if unknown_days:
                _invalid_constraint(
                    constraint,
                    f"DAILY_LIMIT refers to unknown days: {', '.join(unknown_days)}.",
                    add_issue,
                )
                continue
            target_days = (
                tuple(day_by_id[day_id] for day_id in constraint.subject_ids)
                if constraint.subject_ids
                else state.days
            )
            for day in target_days:
                day_entries = [entry for entry in timeline if entry.day_id == day.day_id]
                exceeds_count = (
                    max_activities is not None
                    and len(day_entries) > max_activities
                )
                service_minutes = sum(
                    _elapsed_minutes(entry.start_at, entry.end_at)
                    for entry in day_entries
                )
                exceeds_minutes = (
                    max_minutes is not None
                    and service_minutes > float(max_minutes)
                )
                if exceeds_count or (
                    exceeds_minutes
                    and all(
                        timeline_verified.get(entry.activity_id, False)
                        for entry in day_entries
                    )
                ):
                    _constraint_issue(
                        constraint,
                        "DAILY_LIMIT_EXCEEDED",
                        f"Day {day.day_id!r} exceeds its daily limit.",
                        tuple(entry.activity_id for entry in day_entries),
                        add_issue,
                        ("move_activity_to_another_day",),
                    )
                elif exceeds_minutes:
                    _possible_constraint_issue(
                        constraint,
                        "POSSIBLE_DAILY_LIMIT_EXCEEDED",
                        (
                            f"Unverified durations may cause {day.day_id!r} "
                            "to exceed its daily limit."
                        ),
                        tuple(entry.activity_id for entry in day_entries),
                        add_issue,
                        (
                            "verify_activity_durations",
                            "move_activity_to_another_day",
                        ),
                    )
        elif kind is ConstraintKind.LOCATION_CONTINUITY:
            unknown_days = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id not in day_by_id
            )
            if unknown_days:
                _invalid_constraint(
                    constraint,
                    (
                        "LOCATION_CONTINUITY refers to unknown days: "
                        f"{', '.join(unknown_days)}."
                    ),
                    add_issue,
                )
                continue
            target_days = (
                tuple(day_by_id[day_id] for day_id in constraint.subject_ids)
                if constraint.subject_ids
                else state.days
            )
            ordered_days = sorted(
                target_days, key=lambda item: (item.date, item.day_id)
            )
            for previous, following in zip(ordered_days, ordered_days[1:]):
                continuity_scope = (
                    ("previous_day_id", previous.day_id),
                    ("following_day_id", following.day_id),
                    (
                        "previous_end_location_id",
                        previous.end_location_id,
                    ),
                    (
                        "following_start_location_id",
                        following.start_location_id,
                    ),
                )
                if (
                    previous.end_location_id is None
                    or following.start_location_id is None
                ):
                    _constraint_unknown(
                        constraint,
                        (
                            f"Cannot verify continuity between {previous.day_id!r} "
                            f"and {following.day_id!r} without both base locations."
                        ),
                        add_issue,
                        ("set_day_base_locations",),
                        scope_details=continuity_scope,
                    )
                    continue
                if previous.end_location_id == following.start_location_id:
                    continue
                estimate = _choose_interday_travel(
                    state.travel_estimates, previous, following, now
                )
                if estimate is None:
                    _constraint_issue(
                        constraint,
                        "LOCATION_CONTINUITY_VIOLATION",
                        (
                            f"No transfer connects {previous.day_id!r} end to "
                            f"{following.day_id!r} start."
                        ),
                        (),
                        add_issue,
                        ("add_interday_transfer", "align_day_bases"),
                        scope_details=continuity_scope,
                    )
                    continue
                _check_travel_evidence(estimate, now, None, add_issue)
                allowed_modes = _interday_allowed_modes(previous, following)
                if allowed_modes and estimate.mode not in allowed_modes:
                    _constraint_issue(
                        constraint,
                        "DISALLOWED_MODE",
                        (
                            f"Interday transfer {estimate.mode!r} is not allowed "
                            f"between {previous.day_id!r} and {following.day_id!r}."
                        ),
                        (),
                        add_issue,
                        ("choose_allowed_travel_mode",),
                        scope_details=continuity_scope
                        + (("mode", estimate.mode),),
                    )
                    continue
                _constraint_unknown(
                    constraint,
                    (
                        f"Interday transfer between {previous.day_id!r} and "
                        f"{following.day_id!r} has evidence but no scheduled "
                        "timeline interval."
                    ),
                    add_issue,
                    ("schedule_interday_transfer",),
                    scope_details=continuity_scope
                    + (("mode", estimate.mode),),
                )


def _constraint_issue(
    constraint: Constraint,
    code: str,
    message: str,
    activity_ids: tuple[str, ...],
    add_issue: IssueSink,
    fixes: tuple[str, ...],
    *,
    scope_details: tuple[
        tuple[str, str | int | float | bool | None], ...
    ] = (),
) -> None:
    is_hard = constraint.strength is ConstraintStrength.HARD
    details: tuple[tuple[str, str | int | float | bool | None], ...] = (
        ("constraint_id", constraint.constraint_id),
        ("strength", constraint.strength.value),
    ) + scope_details
    if not is_hard:
        details += (("status_effect", "none"),)
    add_issue(
        _issue(
            code,
            IssueSeverity.ERROR if is_hard else IssueSeverity.WARNING,
            message,
            activity_ids=activity_ids,
            details=details,
            fixes=fixes,
        )
    )


def _invalid_constraint(
    constraint: Constraint, message: str, add_issue: IssueSink
) -> None:
    _constraint_issue(
        constraint,
        "INVALID_CONSTRAINT",
        message,
        constraint.subject_ids,
        add_issue,
        ("repair_constraint",),
    )


def _possible_constraint_issue(
    constraint: Constraint,
    code: str,
    message: str,
    activity_ids: tuple[str, ...],
    add_issue: IssueSink,
    fixes: tuple[str, ...],
) -> None:
    details: tuple[tuple[str, str | int | float | bool | None], ...] = (
        ("constraint_id", constraint.constraint_id),
        ("strength", constraint.strength.value),
    )
    if constraint.strength is ConstraintStrength.SOFT:
        details += (("status_effect", "none"),)
    add_issue(
        _issue(
            code,
            IssueSeverity.WARNING,
            message,
            activity_ids=activity_ids,
            details=details,
            fixes=fixes,
        )
    )


def _constraint_unknown(
    constraint: Constraint,
    message: str,
    add_issue: IssueSink,
    fixes: tuple[str, ...],
    *,
    scope_details: tuple[
        tuple[str, str | int | float | bool | None], ...
    ] = (),
) -> None:
    details: tuple[tuple[str, str | int | float | bool | None], ...] = (
        ("constraint_id", constraint.constraint_id),
        ("strength", constraint.strength.value),
    ) + scope_details
    if constraint.strength is ConstraintStrength.SOFT:
        details += (("status_effect", "none"),)
    add_issue(
        _issue(
            "CONSTRAINT_NEEDS_VERIFICATION",
            IssueSeverity.WARNING,
            message,
            details=details,
            fixes=fixes,
        )
    )


def _derive_status(issues: list[CheckIssue]) -> CheckStatus:
    if any(issue.severity is IssueSeverity.ERROR for issue in issues):
        return CheckStatus.INFEASIBLE
    if any(_requires_verification(issue) for issue in issues):
        return CheckStatus.NEEDS_VERIFICATION
    return CheckStatus.FEASIBLE


def _requires_verification(issue: CheckIssue) -> bool:
    if issue.severity is not IssueSeverity.WARNING:
        return False
    return dict(issue.details).get("status_effect") != "none"


def _is_active(activity: Activity) -> bool:
    return activity.decision_state in _ACTIVE_DECISIONS


def _instant(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _after(left: datetime, right: datetime) -> bool:
    return _instant(left) > _instant(right)


def _latest(values: Iterable[datetime]) -> datetime:
    return max(values, key=_instant)


def _earliest(values: Iterable[datetime]) -> datetime:
    return min(values, key=_instant)


def _add_elapsed(value: datetime, minutes: float) -> datetime:
    zone = value.tzinfo
    if zone is None:
        raise ValueError("elapsed-time arithmetic requires timezone-aware datetime")
    return (
        _instant(value) + timedelta(minutes=minutes)
    ).astimezone(zone)


def _elapsed_minutes(start: datetime, end: datetime) -> float:
    return (_instant(end) - _instant(start)).total_seconds() / 60.0


def _window_on_planning_day(
    day: DaySpec,
    window: TimeWindow,
    zone: ZoneInfo,
    activity_ids: tuple[str, ...],
    add_issue: IssueSink,
) -> tuple[datetime, datetime]:
    start_at = _clock_on_planning_day(
        day.date,
        window.start,
        day.available_start,
        day.available_end,
        zone,
        day.day_id,
        activity_ids,
        add_issue,
    )
    end_date = start_at.date()
    if window.spans_midnight:
        end_date += timedelta(days=1)
    end_at = _localize(
        end_date,
        window.end,
        zone,
        day.day_id,
        activity_ids,
        add_issue,
    )
    return start_at, end_at


def _clock_on_planning_day(
    day_date: date,
    clock: time | None,
    day_start: time | None,
    day_end: time | None,
    zone: ZoneInfo,
    day_id: str,
    activity_ids: tuple[str, ...],
    add_issue: IssueSink,
) -> datetime:
    if clock is None:
        raise ValueError("clock cannot be None")
    target_date = day_date
    if (
        day_start is not None
        and day_end is not None
        and day_end <= day_start
        and clock < day_start
    ):
        target_date += timedelta(days=1)
    return _localize(
        target_date, clock, zone, day_id, activity_ids, add_issue
    )


def _localize(
    local_date: date,
    clock: time,
    zone: ZoneInfo,
    day_id: str,
    activity_ids: tuple[str, ...],
    add_issue: IssueSink,
) -> datetime:
    naive = datetime.combine(local_date, clock)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    first_normalized = first.astimezone(timezone.utc).astimezone(zone)
    second_normalized = second.astimezone(timezone.utc).astimezone(zone)
    first_roundtrip = first_normalized.replace(tzinfo=None)
    second_roundtrip = second_normalized.replace(tzinfo=None)

    if first.utcoffset() != second.utcoffset():
        if first_roundtrip != naive and second_roundtrip != naive:
            add_issue(
                _issue(
                    "NONEXISTENT_LOCAL_TIME",
                    IssueSeverity.ERROR,
                    f"{naive.isoformat()} does not exist in timezone {zone.key}.",
                    activity_ids=activity_ids,
                    details=(
                        ("day_id", day_id),
                        ("local_datetime", naive.isoformat()),
                        ("timezone", zone.key),
                    ),
                    fixes=("move_activity_outside_dst_gap",),
                )
            )
            return first_normalized
        elif first_roundtrip == naive and second_roundtrip == naive:
            add_issue(
                _issue(
                    "AMBIGUOUS_LOCAL_TIME",
                    IssueSeverity.WARNING,
                    f"{naive.isoformat()} is ambiguous in timezone {zone.key}.",
                    activity_ids=activity_ids,
                    details=(
                        ("day_id", day_id),
                        ("local_datetime", naive.isoformat()),
                        ("timezone", zone.key),
                    ),
                    fixes=("specify_dst_fold",),
                )
            )
    return first


def _entry_inside_clock_window(
    entry: TimelineEntry,
    start: time,
    end: time,
    add_issue: IssueSink,
) -> bool:
    zone = entry.start_at.tzinfo
    if not isinstance(zone, ZoneInfo):
        return False
    for start_date in (
        entry.start_at.date(),
        entry.start_at.date() - timedelta(days=1),
    ):
        start_at = _localize(
            start_date,
            start,
            zone,
            entry.day_id,
            (entry.activity_id,),
            add_issue,
        )
        end_date = start_date + (
            timedelta(days=1) if end <= start else timedelta()
        )
        end_at = _localize(
            end_date,
            end,
            zone,
            entry.day_id,
            (entry.activity_id,),
            add_issue,
        )
        if not _after(start_at, entry.start_at) and not _after(
            entry.end_at, end_at
        ):
            return True
    return False


def _choose_interday_travel(
    estimates: Iterable[TravelEstimate],
    previous: DaySpec,
    following: DaySpec,
    now: datetime | None,
) -> TravelEstimate | None:
    if previous.end_location_id is None or following.start_location_id is None:
        return None
    candidates = [
        estimate
        for estimate in estimates
        if estimate.from_location_id == previous.end_location_id
        and estimate.to_location_id == following.start_location_id
        and estimate.day_id in (None, previous.day_id, following.day_id)
        and _route_matches_query_context(estimate, None)
    ]
    if not candidates:
        return None
    allowed_modes = _interday_allowed_modes(previous, following)
    if allowed_modes:
        allowed_candidates = [
            estimate for estimate in candidates if estimate.mode in allowed_modes
        ]
        if allowed_candidates:
            candidates = allowed_candidates
    evidence_rank = {
        EvidenceState.VERIFIED: 0,
        EvidenceState.UNVERIFIED: 1,
        EvidenceState.STALE: 2,
        EvidenceState.CONFLICTED: 3,
    }
    day_rank = {previous.day_id: 0, following.day_id: 1, None: 2}
    candidates.sort(
        key=lambda estimate: (
            0 if _travel_is_verified(estimate, now) else 1,
            0
            if not allowed_modes or estimate.mode in allowed_modes
            else 1,
            day_rank[estimate.day_id],
            evidence_rank[estimate.evidence_state],
            0 if estimate.recommended else 1,
            float(estimate.duration_min) + float(estimate.buffer_min),
            estimate.mode,
            estimate.evidence_ref or "",
        )
    )
    return candidates[0]


def _interday_allowed_modes(
    previous: DaySpec, following: DaySpec
) -> frozenset[str]:
    if previous.allowed_modes:
        return frozenset(previous.allowed_modes)
    return frozenset(following.allowed_modes)


def _parse_constraint_time(value: object) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is None else None


def _csv_param(value: object) -> frozenset[str]:
    if not isinstance(value, str):
        return frozenset()
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _missing_travel_issue(
    day_id: str,
    from_location_id: str,
    to_location_id: str,
    activity_id: str | None,
) -> CheckIssue:
    return _issue(
        "MISSING_TRAVEL_ESTIMATE",
        IssueSeverity.WARNING,
        (
            f"No travel estimate from {from_location_id!r} to "
            f"{to_location_id!r} on {day_id!r}."
        ),
        activity_ids=(activity_id,) if activity_id is not None else (),
        details=(
            ("day_id", day_id),
            ("from_location_id", from_location_id),
            ("to_location_id", to_location_id),
        ),
        fixes=("fetch_travel_estimate",),
    )


def _travel_details(
    estimate: TravelEstimate,
) -> tuple[tuple[str, str | int | float | bool | None], ...]:
    return (
        ("from_location_id", estimate.from_location_id),
        ("to_location_id", estimate.to_location_id),
        ("mode", estimate.mode),
    )


def _evidence_refs(estimate: TravelEstimate) -> tuple[str, ...]:
    return (estimate.evidence_ref,) if estimate.evidence_ref else ()


def _issue(
    code: str,
    severity: IssueSeverity,
    message: str,
    *,
    activity_ids: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    details: tuple[tuple[str, str | int | float | bool | None], ...] = (),
    fixes: tuple[str, ...] = (),
) -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=severity,
        message=message,
        activity_ids=activity_ids,
        evidence_refs=evidence_refs,
        details=details,
        suggested_fixes=fixes,
    )


def _issue_identity(issue: CheckIssue) -> tuple[object, ...]:
    return (
        issue.code,
        issue.severity,
        issue.message,
        issue.activity_ids,
        issue.evidence_refs,
        issue.details,
    )
