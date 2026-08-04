"""Safe presentation model for a browser-facing timeline review.

The planning kernel and ``tripctl`` deliberately expose machine-readable
tokens.  A rendered trip page needs a much smaller, human-readable summary
without carrying through titles, locations, timestamps, IDs, evidence refs,
or raw issue tokens.  This module is the one-way adapter between those two
contracts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


TIMELINE_REVIEW_VIEW_VERSION = "timeline-review-view/v1"

_MAX_AFFECTED_COUNT = 1_000_000


@dataclass(frozen=True, slots=True)
class TimelineReviewItem:
    """One user-facing aggregate that cannot retain a raw kernel token."""

    label: str
    tone: str
    affected_count: int

    def __post_init__(self) -> None:
        if self.tone not in {"warning", "error"}:
            raise ValueError("timeline review item tone is unsupported")
        if type(self.affected_count) is not int or not (
            1 <= self.affected_count <= _MAX_AFFECTED_COUNT
        ):
            raise ValueError("timeline review item count is out of bounds")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "label": self.label,
            "tone": self.tone,
            "affected_count": self.affected_count,
        }


@dataclass(frozen=True, slots=True)
class TimelineReviewView:
    """The complete safe payload consumed by the static trip template."""

    state: str
    status_label: str
    headline: str
    description: str
    next_step: str
    items: tuple[TimelineReviewItem, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in {
            "feasible",
            "needs_verification",
            "infeasible",
            "repair_required",
            "unavailable",
        }:
            raise ValueError("timeline review state is unsupported")
        if any(not isinstance(item, TimelineReviewItem) for item in self.items):
            raise TypeError("timeline review items must be exact values")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": TIMELINE_REVIEW_VIEW_VERSION,
            "state": self.state,
            "status_label": self.status_label,
            "headline": self.headline,
            "description": self.description,
            "next_step": self.next_step,
            "items": [item.to_dict() for item in self.items],
        }


_PRESENTATION = {
    "MISSING_DAY_BOUNDS": ("每日可用時間尚未設定", "warning", 10),
    "MISSING_DURATION": ("景點停留時間尚未補齊", "warning", 20),
    "MISSING_START_LOCATION": ("每日起點尚未設定", "warning", 30),
    "MISSING_END_LOCATION": ("每日終點尚未設定", "warning", 40),
    "MISSING_TRAVEL_ESTIMATE": ("交通時間尚未估算", "warning", 50),
    "INVALID_TRAVEL_REFERENCE": ("部分交通連結需要重建", "warning", 60),
    "POSSIBLE_SCHEDULED_START_CONFLICT": ("可能有預定時間衝突", "warning", 70),
    "TIMEZONE_FALLBACK": ("時區仍使用暫定設定", "warning", 80),
    "UNVERIFIED_EVIDENCE": ("景點或交通資訊尚待確認", "warning", 90),
}

# These are internal/load-level duplicates of a user-facing aggregate above,
# or developer-only compatibility information.  They should not create extra
# cards in a traveler's review page.
_HIDDEN_CODES = frozenset(
    {
        "ACTIVITY_DURATION_UNVERIFIED",
        "SYNTHETIC_ACTIVITY_IDS",
        "TRAVEL_EVIDENCE_UNVERIFIED",
    }
)


def project_timeline_review(payload: object) -> TimelineReviewView:
    """Project one ``tripctl validate`` payload into a safe browser view.

    Invalid, unavailable, or future payload shapes degrade to a fixed generic
    state rather than reflecting any caller-controlled value into HTML.
    """

    if not isinstance(payload, Mapping):
        return _unavailable()
    if payload.get("ok") is not True:
        return _unavailable()
    if payload.get("status") == "repair_required":
        return TimelineReviewView(
            state="repair_required",
            status_label="需要修復資料",
            headline="目前無法完成行程檢查",
            description="行程的必要資料需要先修復，才能重新檢查時間安排。",
            next_step="修復資料後再重新檢查。",
        )

    result = payload.get("result")
    if not isinstance(result, Mapping):
        return _unavailable()
    timeline_status = result.get("timeline_status")
    items = _project_items(payload.get("problems"))
    if timeline_status == "feasible":
        return TimelineReviewView(
            state="feasible",
            status_label="時間線目前可行",
            headline="已完成這次時間線檢查",
            description="目前的時間安排沒有偵測到硬性衝突；仍需依實際行程確認交通與營業資訊。",
            next_step="出發前再確認營業時間、交通與預訂狀態。",
            items=items,
        )
    if timeline_status == "needs_verification":
        return TimelineReviewView(
            state="needs_verification",
            status_label="尚有待確認事項",
            headline="行程還不能視為已準備完成",
            description="這是離線時間線檢查；待確認事項完成前，不代表交通、營業或預訂已確認。",
            next_step="先補齊或確認下列項目，再重新檢查。",
            items=items,
        )
    if timeline_status == "infeasible":
        return TimelineReviewView(
            state="infeasible",
            status_label="需要調整安排",
            headline="時間線偵測到需要處理的衝突",
            description="目前的安排無法直接視為可執行，請先調整衝突或不足的行程資料。",
            next_step="調整行程後再重新檢查。",
            items=items,
        )
    return _unavailable()


def _project_items(raw_problems: object) -> tuple[TimelineReviewItem, ...]:
    if not isinstance(raw_problems, Sequence) or isinstance(
        raw_problems,
        (str, bytes, bytearray),
    ):
        return ()

    grouped: dict[tuple[int, str, str], int] = {}
    for raw_problem in raw_problems:
        if not isinstance(raw_problem, Mapping):
            continue
        code = raw_problem.get("code")
        if not isinstance(code, str) or code in _HIDDEN_CODES:
            continue
        presentation = _PRESENTATION.get(code)
        if presentation is None:
            key = (999, "其他行程資料需要確認", "warning")
        else:
            label, tone, order = presentation
            key = (order, label, tone)
        affected_count = raw_problem.get("affected_count")
        if (
            type(affected_count) is not int
            or not 1 <= affected_count <= _MAX_AFFECTED_COUNT
        ):
            continue
        grouped[key] = min(
            _MAX_AFFECTED_COUNT,
            grouped.get(key, 0) + affected_count,
        )
    return tuple(
        TimelineReviewItem(label, tone, affected_count)
        for (_order, label, tone), affected_count in sorted(grouped.items())
    )


def _unavailable() -> TimelineReviewView:
    return TimelineReviewView(
        state="unavailable",
        status_label="檢查結果尚不可用",
        headline="目前無法顯示行程檢查",
        description="這不會變更行程資料；請在資料完整後重新產生頁面。",
        next_step="完成資料檢查後再重新產生頁面。",
    )


__all__ = [
    "TIMELINE_REVIEW_VIEW_VERSION",
    "TimelineReviewItem",
    "TimelineReviewView",
    "project_timeline_review",
]
