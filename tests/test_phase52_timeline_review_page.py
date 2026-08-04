"""Offline checks for the browser-facing, read-only timeline review tab."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from trip_planner.timeline_review import project_timeline_review
from trip_planner.tripctl import TripctlError, validation_failure


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "template"
RENDER_SCRIPT = REPO_ROOT / "scripts" / "render_trip.py"
TRIPS_ROOT = REPO_ROOT / "trips"
PRIVATE = "private-address-place-id-token-price-37.5000-127.0000"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _render_template(timeline_review: dict[str, object]) -> str:
    template = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR))).get_template(
        "trip.html"
    )
    return template.render(
        trip={
            "slug": "fixture",
            "title": "Fixture trip",
            "subtitle": "offline template check",
            "icon": "🧭",
        },
        itinerary={"days": []},
        info={"sections": []},
        reservations=[],
        packing=[],
        todo=[],
        slug="fixture",
        map_points=[],
        emoji_map={
            "hotel": "🏨",
            "food": "🍜",
            "spot": "📍",
            "drink": "☕",
            "transport": "🚊",
        },
        color_map={
            "hotel": "#e74c3c",
            "work": "#2e86c1",
            "food": "#e67e22",
            "spot": "#27ae60",
            "flight": "#95a5a6",
            "move": "#95a5a6",
        },
        mode_icon={},
        timeline_review=timeline_review,
    )


class Phase52TimelineReviewPageTests(unittest.TestCase):
    def test_template_only_receives_safe_review_projection(self) -> None:
        payload = {
            "ok": True,
            "status": "review_required",
            "result": {"timeline_status": "needs_verification", "private": PRIVATE},
            "problems": [
                {
                    "code": "MISSING_DURATION",
                    "affected_count": 3,
                    "details": PRIVATE,
                },
                {
                    "code": "CALLER_CONTROLLED_PRIVATE_CODE",
                    "affected_count": 2,
                    "title": PRIVATE,
                },
                {
                    "code": "ACTIVITY_DURATION_UNVERIFIED",
                    "affected_count": 8,
                },
            ],
        }

        rendered = _render_template(project_timeline_review(payload).to_dict())

        self.assertIn('data-panel="review"', rendered)
        self.assertIn("['itin','map-panel','checklist','review','info']", rendered)
        self.assertIn("window.location.hash!=='#review'", rendered)
        self.assertIn("景點停留時間尚未補齊", rendered)
        self.assertIn("其他行程資料需要確認", rendered)
        self.assertIn("3 項", rendered)
        self.assertIn("2 項", rendered)
        self.assertNotIn("MISSING_DURATION", rendered)
        self.assertNotIn("CALLER_CONTROLLED_PRIVATE_CODE", rendered)
        self.assertNotIn("ACTIVITY_DURATION_UNVERIFIED", rendered)
        self.assertNotIn(PRIVATE, rendered)

    def test_rejected_validation_degrades_to_fixed_unavailable_view(self) -> None:
        review = project_timeline_review(
            validation_failure(TripctlError("CANONICAL_VALIDATE_UNAVAILABLE"))
        ).to_dict()

        self.assertEqual("unavailable", review["state"])
        self.assertEqual("檢查結果尚不可用", review["status_label"])
        self.assertEqual([], review["items"])
        self.assertNotIn("CANONICAL_VALIDATE_UNAVAILABLE", json.dumps(review))

    def test_real_ishigaki_render_has_review_tab_without_changing_trip_sources(self) -> None:
        before = _tree_bytes(TRIPS_ROOT)
        source_trip = TRIPS_ROOT / "ishigaki-2026-10"

        with tempfile.TemporaryDirectory() as temporary:
            copied_trip = Path(temporary) / source_trip.name
            shutil.copytree(source_trip, copied_trip)
            completed = subprocess.run(
                [sys.executable, str(RENDER_SCRIPT), str(copied_trip)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            rendered = (copied_trip / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-panel="review"', rendered)
        self.assertIn("尚有待確認事項", rendered)
        self.assertIn("行程還不能視為已準備完成", rendered)
        self.assertIn("每日可用時間尚未設定", rendered)
        self.assertIn("景點或交通資訊尚待確認", rendered)
        self.assertNotIn("MISSING_DURATION", rendered)
        self.assertEqual(before, _tree_bytes(TRIPS_ROOT))


if __name__ == "__main__":
    unittest.main()
