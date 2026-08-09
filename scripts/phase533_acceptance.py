#!/usr/bin/env python3
"""Offline Phase 5.33 product acceptance for the Busan/Hokkaido goldens.

The walkthrough creates only temporary canonical stores and uses canned exact
responses.  It never reads repository trips, credentials, or provider data;
it does not render, deploy, or expose a reusable authority token.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trip_planner import (  # noqa: E402
    EvidenceLedger,
    EvidenceSnapshot,
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
    google_maps_policy_registry,
)
from trip_planner.codec import build_plan, encode_plan  # noqa: E402
from trip_planner.guided_canonical_apply import (  # noqa: E402
    GuidedCanonicalApplyResponseKind,
)
from trip_planner.lodging import (  # noqa: E402
    LodgingIntakeAssessment,
    LodgingRequirement,
    assess_lodging_intake,
)
from trip_planner.mutations import ApprovalGrant  # noqa: E402
from trip_planner.store import TripStore  # noqa: E402
from trip_planner.tripctl import (  # noqa: E402
    capture_trip_schedule_apply_response,
    execute_trip_schedule_apply_response,
    inspect_trip,
    prepare_trip_schedule_apply_review,
    propose_trip_with_evidence,
    score_trip_with_evidence,
    validate_trip_with_evidence,
)


ACCEPTANCE_VERSION = "phase533-product-acceptance/v1"
EVALUATION_AT = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
_PRIVATE_SENTINEL = "private-phase533-acceptance-never-output"
_BUSAN_LODGING = "lodging-location-" + "a" * 64
_BUSAN_TERMINAL = "busan-arrival-terminal"
_BUSAN_DINNER = "busan-dinner-location"
_BUSAN_DAY_3_ACTIVITY = "busan-day-3-activity-location"
_HOKKAIDO_HOTEL = "lodging-location-" + "b" * 64
_HOKKAIDO_RYOKAN = "lodging-location-" + "c" * 64


class _SnapshotLoad:
    def __init__(self, snapshot: EvidenceSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        if evaluation_at != self._snapshot.evaluation_at:
            raise RuntimeError("acceptance evidence clock changed")
        return self._snapshot


class _EvidenceSource:
    def __init__(self, snapshot: EvidenceSnapshot) -> None:
        self._snapshot = snapshot
        self.load_count = 0

    def load(self) -> _SnapshotLoad:
        self.load_count += 1
        return _SnapshotLoad(self._snapshot)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _snapshot() -> EvidenceSnapshot:
    ledger = EvidenceLedger(
        google_maps_policy_registry(GOOGLE_MAPS_NON_EEA_POLICY_PROFILE)
    )
    return EvidenceSnapshot.from_ledger(
        ledger,
        evaluation_at=EVALUATION_AT,
        purge_now=EVALUATION_AT,
    )


def _lodging_intake(
    first_day: date,
) -> LodgingIntakeAssessment:
    return assess_lodging_intake(
        stay_start=first_day,
        stay_end=first_day + timedelta(days=3),
        requirement=LodgingRequirement.REQUIRED,
    )


def _movable_activity(
    activity_id: str,
    *,
    start: str,
    location_id: str,
    window: tuple[str, str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "activity_id": activity_id,
        "title": _PRIVATE_SENTINEL,
        "location_id": location_id,
        "time": start,
        "duration_min": 30,
        "decision_state": "selected",
        "flexibility": "movable",
        "evidence_state": "verified",
        "type": "activity",
    }
    if window is not None:
        result["allowed_windows"] = [
            {"start": window[0], "end": window[1]}
        ]
    return result


def _booked_activity(
    activity_id: str,
    *,
    start: str,
    location_id: str,
) -> dict[str, Any]:
    return {
        "activity_id": activity_id,
        "title": _PRIVATE_SENTINEL,
        "location_id": location_id,
        "time": start,
        "duration_min": 30,
        "decision_state": "booked",
        "flexibility": "fixed_time",
        "evidence_state": "verified",
        "type": "activity",
    }


def _fixed_anchor_activity(
    activity_id: str,
    *,
    start: str,
    location_id: str,
) -> dict[str, Any]:
    return {
        "activity_id": activity_id,
        "title": _PRIVATE_SENTINEL,
        "location_id": location_id,
        "time": start,
        "duration_min": 1,
        "decision_state": "selected",
        "flexibility": "fixed_time",
        "evidence_state": "verified",
        "type": "anchor",
    }


def _optimization_day(
    *,
    day_id: str,
    day_number: int,
    day_date: date,
    timezone_name: str,
    start_location_id: str,
    end_lodging_id: str,
    end_location_id: str,
    start_lodging_id: str | None = None,
    activity_location_id: str | None = None,
    return_to_end_anchor: bool = False,
) -> dict[str, Any]:
    middle_location_id = activity_location_id or end_location_id
    result = {
        "day_id": day_id,
        "day": day_number,
        "date": day_date.isoformat(),
        "timezone": timezone_name,
        "available_start": "08:00",
        "available_end": "20:00",
        "start_location_id": start_location_id,
        "end_location_id": end_location_id,
        "end_lodging_id": end_lodging_id,
        "allowed_modes": ["walking"],
        "places": [
            _movable_activity(
                "schedule-alpha",
                start="11:00",
                location_id=start_location_id,
                window=("09:00", "10:00"),
            ),
            _movable_activity(
                "schedule-beta",
                start="12:00",
                location_id=middle_location_id,
            ),
        ],
        "travel": [
            {
                "from_activity_id": "schedule-alpha",
                "to_activity_id": "schedule-beta",
                "recommended_mode": "walking",
                "modes": {
                    "walking": {
                        "duration_min": (
                            0
                            if start_location_id == middle_location_id
                            else 10
                        ),
                        "evidence_state": "verified",
                    }
                },
            }
        ],
    }
    if return_to_end_anchor:
        result["places"].append(
            _fixed_anchor_activity(
                f"{day_id}-schedule-return-anchor",
                start="13:00",
                location_id=end_location_id,
            )
        )
        result["travel"].append(
            {
                "from_activity_id": "schedule-beta",
                "to_activity_id": f"{day_id}-schedule-return-anchor",
                "recommended_mode": "walking",
                "modes": {
                    "walking": {
                        "duration_min": (
                            0
                            if middle_location_id == end_location_id
                            else 10
                        ),
                        "evidence_state": "verified",
                    }
                },
            }
        )
    if start_lodging_id is not None:
        result["start_lodging_id"] = start_lodging_id
    return result


def _busan_plan() -> tuple[dict[str, Any], date]:
    first = date(2026, 10, 1)
    trip = {
        "slug": "busan-phase533",
        "title": "Busan Phase 5.33 acceptance",
        "timezone": "Asia/Seoul",
        "date_range": "2026-10-01 ~ 2026-10-03",
        "cities": ["Busan"],
        "constraints": [],
        "lodgings": [
            {
                "lodging_id": "busan-stay-a",
                "location_id": _BUSAN_LODGING,
                "check_in": "2026-10-01",
                "check_out": "2026-10-04",
                "kind": "hotel",
                "decision_state": "selected",
                "evidence_state": "unverified",
            }
        ],
        "_trip_planner": {
            "migration": {
                "source_schema": "legacy-v1",
                "source_revision": "busan-acceptance-source",
                "protected_activity_ids": [
                    "schedule-alpha",
                    "schedule-beta",
                ],
                "ignored_travel_edges": [],
            }
        },
    }
    day_1 = {
        "day_id": "day-1",
        "day": 1,
        "date": first.isoformat(),
        "timezone": "Asia/Seoul",
        "available_start": "08:00",
        "available_end": "22:00",
        "start_location_id": _BUSAN_TERMINAL,
        "end_location_id": _BUSAN_LODGING,
        "end_lodging_id": "busan-stay-a",
        "allowed_modes": ["walking"],
        "places": [
            _booked_activity(
                "busan-booked-arrival",
                start="10:00",
                location_id=_BUSAN_TERMINAL,
            ),
            _fixed_anchor_activity(
                "busan-arrival-to-stay-anchor",
                start="10:45",
                location_id=_BUSAN_LODGING,
            ),
        ],
        "travel": [
            {
                "from_activity_id": "busan-booked-arrival",
                "to_activity_id": "busan-arrival-to-stay-anchor",
                "recommended_mode": "walking",
                "modes": {
                    "walking": {
                        "duration_min": 12,
                        "evidence_state": "verified",
                    }
                },
            }
        ],
    }
    day_2 = {
        "day_id": "day-2",
        "day": 2,
        "date": (first + timedelta(days=1)).isoformat(),
        "timezone": "Asia/Seoul",
        "available_start": "08:00",
        "available_end": "22:00",
        "start_location_id": _BUSAN_LODGING,
        "end_location_id": _BUSAN_LODGING,
        "start_lodging_id": "busan-stay-a",
        "end_lodging_id": "busan-stay-a",
        "allowed_modes": ["walking"],
        "places": [
            _fixed_anchor_activity(
                "busan-dinner-departure-anchor",
                start="17:30",
                location_id=_BUSAN_LODGING,
            ),
            _booked_activity(
                "busan-booked-dinner",
                start="18:00",
                location_id=_BUSAN_DINNER,
            ),
            _fixed_anchor_activity(
                "busan-dinner-return-anchor",
                start="19:00",
                location_id=_BUSAN_LODGING,
            ),
        ],
        "travel": [
            {
                "from_activity_id": "busan-dinner-departure-anchor",
                "to_activity_id": "busan-booked-dinner",
                "recommended_mode": "walking",
                "modes": {
                    "walking": {
                        "duration_min": 12,
                        "evidence_state": "verified",
                    }
                },
            },
            {
                "from_activity_id": "busan-booked-dinner",
                "to_activity_id": "busan-dinner-return-anchor",
                "recommended_mode": "walking",
                "modes": {
                    "walking": {
                        "duration_min": 12,
                        "evidence_state": "verified",
                    }
                },
            }
        ],
    }
    day_3 = _optimization_day(
        day_id="day-3",
        day_number=3,
        day_date=first + timedelta(days=2),
        timezone_name="Asia/Seoul",
        start_location_id=_BUSAN_LODGING,
        start_lodging_id="busan-stay-a",
        end_lodging_id="busan-stay-a",
        end_location_id=_BUSAN_LODGING,
        activity_location_id=_BUSAN_DAY_3_ACTIVITY,
        return_to_end_anchor=True,
    )
    return (
        build_plan(
            trip_id="busan-phase533",
            generation=1,
            state={
                "trip": trip,
                "itinerary": {
                    "available_modes": ["walking"],
                    "days": [day_1, day_2, day_3],
                },
            },
        ),
        first,
    )


def _hokkaido_plan() -> tuple[dict[str, Any], date]:
    first = date(2026, 12, 10)
    trip = {
        "slug": "hokkaido-phase533",
        "title": "Hokkaido Phase 5.33 acceptance",
        "timezone": "Asia/Tokyo",
        "date_range": "2026-12-10 ~ 2026-12-12",
        "cities": ["Hokkaido"],
        "constraints": [],
        "lodgings": [
            {
                "lodging_id": "hokkaido-stay-a",
                "location_id": _HOKKAIDO_HOTEL,
                "check_in": "2026-12-10",
                "check_out": "2026-12-11",
                "kind": "hotel",
                "decision_state": "selected",
                "evidence_state": "unverified",
            },
            {
                "lodging_id": "hokkaido-stay-b",
                "location_id": _HOKKAIDO_RYOKAN,
                "check_in": "2026-12-11",
                "check_out": "2026-12-13",
                "kind": "ryokan",
                "decision_state": "booked",
                "evidence_state": "unverified",
            },
        ],
    }
    day_2 = {
        "day_id": "day-2",
        "day": 2,
        "date": "2026-12-11",
        "timezone": "Asia/Tokyo",
        "available_start": "08:00",
        "available_end": "22:00",
        "start_location_id": _HOKKAIDO_HOTEL,
        "end_location_id": _HOKKAIDO_RYOKAN,
        "start_lodging_id": "hokkaido-stay-a",
        "end_lodging_id": "hokkaido-stay-b",
        "allowed_modes": ["walking", "driving"],
        "places": [
            _booked_activity(
                "hokkaido-transfer-start",
                start="08:00",
                location_id=_HOKKAIDO_HOTEL,
            ),
            _booked_activity(
                "hokkaido-booked-checkin",
                start="16:00",
                location_id=_HOKKAIDO_RYOKAN,
            ),
        ],
        "travel": [
            {
                "from_activity_id": "hokkaido-transfer-start",
                "to_activity_id": "hokkaido-booked-checkin",
                "recommended_mode": "driving",
                "modes": {
                    "driving": {
                        "duration_min": 180,
                        "buffer_min": 60,
                        "evidence_state": "verified",
                    }
                },
            }
        ],
    }
    day_3 = {
        "day_id": "day-3",
        "day": 3,
        "date": "2026-12-12",
        "timezone": "Asia/Tokyo",
        "available_start": "08:00",
        "available_end": "22:00",
        "start_location_id": _HOKKAIDO_RYOKAN,
        "end_location_id": _HOKKAIDO_RYOKAN,
        "start_lodging_id": "hokkaido-stay-b",
        "end_lodging_id": "hokkaido-stay-b",
        "allowed_modes": ["walking", "driving"],
        "places": [],
        "travel": [],
    }
    return (
        build_plan(
            trip_id="hokkaido-phase533",
            generation=1,
            state={
                "trip": trip,
                "itinerary": {
                    "available_modes": ["walking", "driving"],
                    "days": [
                        _optimization_day(
                            day_id="day-1",
                            day_number=1,
                            day_date=first,
                            timezone_name="Asia/Tokyo",
                            start_location_id="schedule-origin",
                            end_lodging_id="hokkaido-stay-a",
                            end_location_id=_HOKKAIDO_HOTEL,
                        ),
                        day_2,
                        day_3,
                    ],
                },
            },
        ),
        first,
    )


def _fixed_times(plan: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(place["time"])
        for day in plan["state"]["itinerary"]["days"]
        for place in day["places"]
        if place["decision_state"] == "booked"
    )


def _execute_case(
    root: Path,
    *,
    slug: str,
    plan: dict[str, Any],
    first_day: date,
    fixed_times: tuple[str, ...],
    approval_resume: bool,
    inject_lost_ack: bool,
) -> dict[str, Any]:
    data_dir = root / slug / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "plan.json").write_bytes(encode_plan(plan))
    lost_ack_count = 0

    def fault(stage: str) -> None:
        nonlocal lost_ack_count
        if inject_lost_ack and stage == "after_replace":
            lost_ack_count += 1
            raise RuntimeError("canned lost acknowledgement")

    store = TripStore(
        root,
        slug,
        fault_hook=fault if inject_lost_ack else None,
    )
    snapshot = _snapshot()
    source = _EvidenceSource(snapshot)
    lodging_intake = _lodging_intake(first_day)
    inspection = inspect_trip(data_dir)
    validation = validate_trip_with_evidence(
        data_dir,
        evidence_snapshot=snapshot,
        lodging_intake=lodging_intake,
    )
    proposal = propose_trip_with_evidence(
        data_dir,
        evidence_snapshot=snapshot,
        lodging_intake=lodging_intake,
    )
    proposal_ref = proposal["result"]["proposal_ref"]
    _require(isinstance(proposal_ref, str), "proposal ref missing")
    score = score_trip_with_evidence(
        data_dir,
        proposal_ref=proposal_ref,
        evidence_snapshot=snapshot,
        lodging_intake=lodging_intake,
    )
    resumed_score = score_trip_with_evidence(
        data_dir,
        proposal_ref=proposal_ref,
        evidence_snapshot=snapshot,
        lodging_intake=lodging_intake,
    )
    review = prepare_trip_schedule_apply_review(
        store,
        proposal_ref=proposal_ref,
        evidence_snapshot=snapshot,
        evidence_source=source,
        lodging_intake=lodging_intake,
        reviewed_at=EVALUATION_AT + timedelta(seconds=1),
        run_id=f"{slug}-phase533-acceptance",
    )
    before_capture = (data_dir / "plan.json").read_bytes()
    response = capture_trip_schedule_apply_response(
        review,
        GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
        evaluation_at=EVALUATION_AT + timedelta(seconds=2),
    )
    _require(
        before_capture == (data_dir / "plan.json").read_bytes(),
        "response capture wrote canonical state",
    )

    waiting = None
    approval_wait_contract_confirmed = False
    if approval_resume:
        waiting = execute_trip_schedule_apply_response(
            review,
            response,
            store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        ).to_dict()
        _require(
            waiting["status"] == "waiting_approval"
            and waiting["next_action"] == "obtain_exact_authority"
            and waiting["requires_user_review"] is True
            and waiting["retryable"] is False
            and waiting["pending_review_retained"] is True
            and response.terminal is False,
            "approval resume did not retain the exact response",
        )
        _require(
            before_capture == (data_dir / "plan.json").read_bytes(),
            "missing approval wrote canonical state",
        )
        review_projection = review.to_dict()
        scope = review_projection["result"]["apply_review"]["preview"][
            "required_approval_scope"
        ]
        _require(isinstance(scope, str), "approval scope missing")
        approval_wait_contract_confirmed = True
        approvals = (
            ApprovalGrant(
                approval_id="phase533-canned-store-approval",
                scope_digest=scope,
                approved_by="phase533-canned-host",
                approved_at=(
                    EVALUATION_AT + timedelta(seconds=3)
                ).isoformat(),
            ),
        )
        execution_at = EVALUATION_AT + timedelta(seconds=4)
    else:
        approvals = ()
        execution_at = EVALUATION_AT + timedelta(seconds=3)

    outcome = execute_trip_schedule_apply_response(
        review,
        response,
        store,
        evaluation_at=execution_at,
        approvals=approvals,
    ).to_dict()
    current = store.load_plan()
    post_apply = validate_trip_with_evidence(
        data_dir,
        evidence_snapshot=snapshot,
        lodging_intake=lodging_intake,
    )
    schedule_alpha = next(
        place
        for day in current["state"]["itinerary"]["days"]
        for place in day["places"]
        if place["activity_id"] == "schedule-alpha"
    )
    result = {
        "status_chain": {
            "inspect": inspection["status"],
            "validate": validation["status"],
            "propose": proposal["status"],
            "score": score["status"],
            "review": review.to_dict()["status"],
            "response": response.to_dict()["status"],
            "execute": outcome["status"],
            "post_apply_validate": post_apply["status"],
        },
        "next_action": outcome["next_action"],
        "same_context_score_replay_confirmed": score == resumed_score,
        "process_local_response_terminal": response.terminal,
        "capture_performed_zero_writes": True,
        "schedule_change_applied": schedule_alpha["time"] == "09:00:00",
        "fixed_activity_times_preserved": _fixed_times(current) == fixed_times,
        "canonical_write_outcome": outcome["result"][
            "canonical_write_outcome"
        ],
        "canonical_write_performed": outcome["result"][
            "canonical_write_performed"
        ],
        "receipt_count": len(current["receipts"]),
        "approval_resume_confirmed": (
            approval_wait_contract_confirmed
            and waiting is not None
            and waiting["status"] == "waiting_approval"
            and outcome["status"] == "waiting_external"
        ),
        "approval_wait_contract_confirmed": (
            approval_wait_contract_confirmed
        ),
        "lost_ack_injected": inject_lost_ack,
        "lost_ack_count": lost_ack_count,
        "receipt_reconciliation_confirmed": outcome["result"][
            "replay_confirmed"
        ],
    }
    safe_payload = json.dumps(
        {
            "inspection": inspection,
            "validation": validation,
            "proposal": proposal,
            "score": score,
            "review": review.to_dict(),
            "response": response.to_dict(),
            "outcome": outcome,
            "post_apply": post_apply,
            "result": result,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    _require(_PRIVATE_SENTINEL not in safe_payload, "private value leaked")
    _require(
        outcome["status"] == "waiting_external"
        and outcome["next_action"] == "refresh_external_evidence"
        and outcome["result"]["canonical_write_performed"] is True,
        "successful apply outcome was not truthful",
    )
    _require(
        inspection["status"] == "waiting_external"
        and validation["status"] == "draft"
        and post_apply["status"] == "review",
        "product validation status transition changed",
    )
    _require(len(current["receipts"]) == 1, "receipt count changed")
    return result


def run_walkthrough() -> dict[str, Any]:
    """Return a redacted, deterministic acceptance transcript."""

    with tempfile.TemporaryDirectory(
        prefix="trip-planner-phase533-"
    ) as temporary:
        root = Path(temporary)
        busan_plan, busan_first = _busan_plan()
        hokkaido_plan, hokkaido_first = _hokkaido_plan()
        busan = _execute_case(
            root,
            slug="busan-phase533",
            plan=busan_plan,
            first_day=busan_first,
            fixed_times=("10:00", "18:00"),
            approval_resume=True,
            inject_lost_ack=False,
        )
        current_busan = TripStore(root, "busan-phase533").load_plan()
        busan_days = current_busan["state"]["itinerary"]["days"]
        busan_day_1_places = {
            item["activity_id"]: item for item in busan_days[0]["places"]
        }
        busan_day_2_places = {
            item["activity_id"]: item for item in busan_days[1]["places"]
        }
        busan_arrival = busan_day_1_places["busan-booked-arrival"]
        busan_dinner = busan_day_2_places["busan-booked-dinner"]
        busan["daily_lodging_anchors_preserved"] = all(
            day.get("end_lodging_id") == "busan-stay-a"
            and (
                index == 0
                or day.get("start_lodging_id") == "busan-stay-a"
            )
            for index, day in enumerate(busan_days)
        )
        busan["arrival_day_boundary_preserved"] = (
            busan_days[0]["date"] == busan_first.isoformat()
            and busan_days[0]["start_location_id"] == _BUSAN_TERMINAL
            and busan_arrival["location_id"] == _BUSAN_TERMINAL
            and busan_arrival["time"] == "10:00"
        )
        busan["dinner_day_boundary_preserved"] = (
            busan_days[1]["date"]
            == (busan_first + timedelta(days=1)).isoformat()
            and busan_dinner["location_id"] == _BUSAN_DINNER
            and busan_dinner["time"] == "18:00"
        )
        busan["semantic_locations_distinct"] = len(
            {_BUSAN_TERMINAL, _BUSAN_LODGING, _BUSAN_DINNER}
        ) == 3
        busan["scheduler_preserved_migration_protection"] = (
            current_busan["state"]["trip"]["_trip_planner"]["migration"][
                "protected_activity_ids"
            ]
            == ["schedule-alpha", "schedule-beta"]
        )

        hokkaido = _execute_case(
            root,
            slug="hokkaido-phase533",
            plan=hokkaido_plan,
            first_day=hokkaido_first,
            fixed_times=("08:00", "16:00"),
            approval_resume=False,
            inject_lost_ack=True,
        )
        current_hokkaido = TripStore(root, "hokkaido-phase533").load_plan()
        hokkaido_day_2 = current_hokkaido["state"]["itinerary"]["days"][1]
        driving = hokkaido_day_2["travel"][0]["modes"]["driving"]
        hokkaido.update(
            {
                "split_stay_anchor_preserved": (
                    hokkaido_day_2["start_lodging_id"]
                    == "hokkaido-stay-a"
                    and hokkaido_day_2["end_lodging_id"]
                    == "hokkaido-stay-b"
                ),
                "cross_city_duration_preserved": (
                    driving["duration_min"] == 180
                ),
                "winter_buffer_preserved": driving["buffer_min"] >= 45,
            }
        )
        transcript = {
            "contract_version": ACCEPTANCE_VERSION,
            "mode": "offline_temporary_canned_authority",
            "boundaries": {
                "provider_calls": 0,
                "credential_reads": 0,
                "repository_trip_writes": 0,
                "rendered": False,
                "deployed": False,
                "serialized_authority": False,
            },
            "exercised_resume_paths": {
                "same_context_score_replay": True,
                "retained_response_after_missing_approval": True,
            },
            "busan": busan,
            "hokkaido": hokkaido,
        }
    rendered = json.dumps(transcript, ensure_ascii=False, sort_keys=True)
    _require(_PRIVATE_SENTINEL not in rendered, "transcript leaked private text")
    return transcript


def main() -> None:
    print(
        json.dumps(
            run_walkthrough(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
