"""User-readable offline acceptance contract for the Phase 4.5 workflow."""

from __future__ import annotations

import json
import unittest

from scripts.phase45_acceptance import run_walkthrough


class Phase45AcceptanceTests(unittest.TestCase):
    def test_busan_and_hokkaido_walkthrough(self) -> None:
        transcript = run_walkthrough()

        busan = transcript["釜山直接指定住宿"]
        self.assertEqual("waiting_confirmation", busan["未確認結果"])
        self.assertEqual("applied", busan["套用結果"])
        self.assertEqual("replay_confirmed", busan["收據重播"])
        self.assertEqual(["10:00", "18:00"], busan["已訂活動時間保持"])
        self.assertTrue(busan["canonical、收據與歷史無私密原文"])

        hokkaido = transcript["北海道換宿"]
        self.assertEqual("applied", hokkaido["套用結果"])
        self.assertEqual(["16:00"], hokkaido["已訂活動時間保持"])
        self.assertTrue(hokkaido["換宿日不同錨點"])
        self.assertTrue(hokkaido["canonical、收據與歷史無私密原文"])

    def test_walkthrough_is_redacted_and_offline_labeled(self) -> None:
        transcript = run_walkthrough()
        rendered = json.dumps(transcript, ensure_ascii=False, sort_keys=True)

        self.assertEqual(
            "完全離線、暫存資料、developer preview",
            transcript["模式"],
        )
        self.assertIn(
            "禁止用於production",
            transcript["安全界線"],
        )
        for private_value in (
            "私人住宿地址與價格絕不輸出",
            "私人札幌第一晚住宿",
            "私人溫泉旅館地址",
        ):
            self.assertNotIn(private_value, rendered)


if __name__ == "__main__":
    unittest.main()
