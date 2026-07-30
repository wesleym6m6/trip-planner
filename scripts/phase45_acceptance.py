#!/usr/bin/env python3
"""Offline Phase 4.5D acceptance walkthrough.

Creates only temporary canonical plans.  It neither contacts providers nor
reads/writes repository trips, renders output, or deploys anything.
Its embedded demo signer must never be imported into a production TripStore.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trip_planner.codec import build_plan, encode_plan
from trip_planner.lodging import LodgingKind
from trip_planner.lodging_confirmation import (
    LodgingConfirmationAuthority,
    LodgingConfirmationRequest,
    LodgingConfirmationStager,
    LodgingConfirmationState,
    LodgingSelectionAnchor,
    LodgingSelectionSegment,
)
from trip_planner.models import DecisionState
from trip_planner.store import TripStore


NOW = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)
_SIGNING_KEY = b"phase45-acceptance-demo-host"
_ISSUER = "phase45-acceptance-host"
_BUSAN_PRIVATE = "私人住宿地址與價格絕不輸出"
_HOKKAIDO_PRIVATE = (
    "私人札幌第一晚住宿",
    "私人溫泉旅館地址",
)


def _sign(payload: bytes) -> str:
    return hmac.new(_SIGNING_KEY, payload, hashlib.sha256).hexdigest()


def _verify(grant: object) -> bool:
    """Demo-only host verifier; production signer/verifier stays external."""

    try:
        return (
            grant.issuer_id == _ISSUER
            and grant.confirmed_at <= NOW <= grant.expires_at
            and hmac.compare_digest(grant.signature, _sign(grant.verification_payload()))
        )
    except (AttributeError, TypeError):
        return False


def _state(*, split_stay: bool) -> dict[str, Any]:
    first = date(2026, 12, 10) if split_stay else date(2026, 8, 1)
    days: list[dict[str, Any]] = []
    for offset in range(3):
        places: list[dict[str, Any]] = []
        if not split_stay and offset == 0:
            places.append(_booked("booked-arrival", "抵達", "10:00"))
        if not split_stay and offset == 1:
            places.append(_booked("booked-dinner", "已訂晚餐", "18:00"))
        if split_stay and offset == 1:
            places.append(_booked("booked-ryokan-checkin", "已訂旅館入住", "16:00"))
        days.append(
            {
                "day_id": f"day-{offset + 1}",
                "date": (first + timedelta(days=offset)).isoformat(),
                "timezone": "Asia/Tokyo",
                "available_start": "08:00",
                "available_end": "22:00",
                "places": places,
                "travel": [],
            }
        )
    return {"trip": {"trip_id": "acceptance", "title": "offline"}, "itinerary": {"days": days}}


def _booked(activity_id: str, title: str, at: str) -> dict[str, Any]:
    return {
        "activity_id": activity_id,
        "title": title,
        "location_id": f"location-{activity_id}",
        "time": at,
        "duration_min": 30,
        "decision_state": "booked",
        "flexibility": "fixed_time",
        "evidence_state": "verified",
    }


def _store(root: Path, slug: str, *, split_stay: bool) -> TripStore:
    data = root / slug / "data"
    data.mkdir(parents=True)
    plan = build_plan(trip_id="acceptance", generation=1, state=_state(split_stay=split_stay))
    (data / "plan.json").write_bytes(encode_plan(plan))
    return TripStore(root, slug, lodging_confirmation_verifier=_verify)


def _request(store: TripStore, *, split_stay: bool) -> LodgingConfirmationRequest:
    plan = store.load_plan()
    if not split_stay:
        # The raw text is immediately converted to a fresh opaque location ID.
        stay = LodgingSelectionSegment(
            location_id=_BUSAN_PRIVATE,
            check_in=date(2026, 8, 1), check_out=date(2026, 8, 4),
            kind=LodgingKind.HOTEL, decision_state=DecisionState.SELECTED,
        )
        stays = (stay,)
        anchors = (
            LodgingSelectionAnchor("day-1", end_lodging_id=stay.lodging_id),
            LodgingSelectionAnchor("day-2", stay.lodging_id, stay.lodging_id),
            LodgingSelectionAnchor("day-3", stay.lodging_id, stay.lodging_id),
        )
    else:
        first = LodgingSelectionSegment(
            location_id=_HOKKAIDO_PRIVATE[0],
            check_in=date(2026, 12, 10), check_out=date(2026, 12, 11),
            kind=LodgingKind.HOTEL, decision_state=DecisionState.SELECTED,
        )
        second = LodgingSelectionSegment(
            location_id=_HOKKAIDO_PRIVATE[1],
            check_in=date(2026, 12, 11), check_out=date(2026, 12, 13),
            kind=LodgingKind.RYOKAN, decision_state=DecisionState.BOOKED,
        )
        stays = (first, second)
        anchors = (
            LodgingSelectionAnchor("day-1", end_lodging_id=first.lodging_id),
            LodgingSelectionAnchor("day-2", first.lodging_id, second.lodging_id),
            LodgingSelectionAnchor("day-3", second.lodging_id, second.lodging_id),
        )
    return LodgingConfirmationRequest(
        trip_id="acceptance", base_revision=plan["revision"],
        idempotency_key=f"acceptance-{'hokkaido' if split_stay else 'busan'}",
        segments=stays, anchors=anchors, evaluation_at=NOW,
    )


def _authority() -> LodgingConfirmationAuthority:
    return LodgingConfirmationAuthority(
        confirmed_by="acceptance-host", issuer_id=_ISSUER, signer=_sign,
        clock=lambda: NOW,
    )


def _case(root: Path, slug: str, *, split_stay: bool) -> dict[str, Any]:
    store = _store(root, slug, split_stay=split_stay)
    request = _request(store, split_stay=split_stay)
    stager = LodgingConfirmationStager(store, clock=lambda: NOW)
    review = stager.stage(request)
    missing = stager.commit(review.review_id, confirmation=None)
    applied = stager.commit(review.review_id, confirmation=_authority().issue_grant(review))
    plan = store.load_plan()
    days = plan["state"]["itinerary"]["days"]
    replay = LodgingConfirmationStager(store, clock=lambda: NOW).stage(request)
    protected_times = [place["time"] for day in days for place in day["places"]]
    private_values = (
        _HOKKAIDO_PRIVATE if split_stay else (_BUSAN_PRIVATE,)
    )
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / slug / "data").rglob("*"))
        if path.is_file()
    )
    artifacts_redacted = not any(
        private_value in persisted for private_value in private_values
    )
    if not artifacts_redacted:
        raise AssertionError(
            "walkthrough persisted a private lodging input"
        )
    return {
        "審閱狀態": review.state.value,
        "未確認結果": missing.state.value,
        "套用結果": applied.state.value,
        "收據重播": replay.state.value,
        "住宿段數": len(plan["state"]["trip"]["lodgings"]),
        "已訂活動時間保持": protected_times,
        "換宿日不同錨點": (
            days[1].get("start_lodging_id") != days[1].get("end_lodging_id")
            if split_stay else None
        ),
        "canonical、收據與歷史無私密原文": artifacts_redacted,
    }


def run_walkthrough() -> dict[str, Any]:
    """Return a concise, redacted Traditional-Chinese acceptance transcript."""

    with tempfile.TemporaryDirectory(prefix="trip-planner-phase45-") as temporary:
        root = Path(temporary)
        transcript = {
            "版本": "phase45-acceptance/v1",
            "模式": "完全離線、暫存資料、developer preview",
            "安全界線": "內建demo signer禁止用於production TripStore",
            "釜山直接指定住宿": _case(root, "busan", split_stay=False),
            "北海道換宿": _case(root, "hokkaido", split_stay=True),
            "既有回歸覆蓋（非本walkthrough執行）": {
                "同分不會自動選擇": "需由使用者明選後才可建立確認請求",
                "缺少或過期證據": "不可由4.5C投影成可套用選擇",
                "偽造確認與lost ACK": "fail closed並可用exact receipt安全重試",
            },
        }
    encoded = json.dumps(transcript, ensure_ascii=False, sort_keys=True)
    for secret in (_BUSAN_PRIVATE, *_HOKKAIDO_PRIVATE):
        if secret in encoded:
            raise AssertionError("walkthrough transcript leaked private lodging input")
    return transcript


def main() -> None:
    print(json.dumps(run_walkthrough(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
