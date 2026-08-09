"""Offline Phase 5.33 product-facade acceptance."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.phase533_acceptance import run_walkthrough


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "phase533_acceptance.py"


class Phase533ProductAcceptanceTests(unittest.TestCase):
    def test_semantic_goldens_and_truthful_apply_resume(self) -> None:
        transcript = run_walkthrough()
        busan = transcript["busan"]
        hokkaido = transcript["hokkaido"]

        for case in (busan, hokkaido):
            self.assertEqual(
                "waiting_external",
                case["status_chain"]["inspect"],
            )
            self.assertEqual("draft", case["status_chain"]["validate"])
            self.assertEqual("ready", case["status_chain"]["propose"])
            self.assertEqual(
                "review_required",
                case["status_chain"]["score"],
            )
            self.assertEqual(
                "review_required",
                case["status_chain"]["review"],
            )
            self.assertEqual(
                "response_captured",
                case["status_chain"]["response"],
            )
            self.assertEqual(
                "waiting_external",
                case["status_chain"]["execute"],
            )
            self.assertEqual(
                "review",
                case["status_chain"]["post_apply_validate"],
            )
            self.assertEqual(
                "refresh_external_evidence",
                case["next_action"],
            )
            self.assertTrue(case["same_context_score_replay_confirmed"])
            self.assertTrue(case["process_local_response_terminal"])
            self.assertTrue(case["capture_performed_zero_writes"])
            self.assertTrue(case["schedule_change_applied"])
            self.assertTrue(case["fixed_activity_times_preserved"])
            self.assertEqual("performed", case["canonical_write_outcome"])
            self.assertTrue(case["canonical_write_performed"])
            self.assertEqual(1, case["receipt_count"])

        self.assertTrue(busan["approval_resume_confirmed"])
        self.assertTrue(busan["approval_wait_contract_confirmed"])
        self.assertTrue(busan["daily_lodging_anchors_preserved"])
        self.assertTrue(busan["arrival_day_boundary_preserved"])
        self.assertTrue(busan["dinner_day_boundary_preserved"])
        self.assertTrue(busan["semantic_locations_distinct"])
        self.assertTrue(busan["scheduler_preserved_migration_protection"])
        self.assertFalse(busan["lost_ack_injected"])

        self.assertTrue(hokkaido["lost_ack_injected"])
        self.assertEqual(1, hokkaido["lost_ack_count"])
        self.assertTrue(hokkaido["receipt_reconciliation_confirmed"])
        self.assertTrue(hokkaido["split_stay_anchor_preserved"])
        self.assertTrue(hokkaido["cross_city_duration_preserved"])
        self.assertTrue(hokkaido["winter_buffer_preserved"])

    def test_transcript_is_redacted_offline_and_cli_json_only(self) -> None:
        call = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, call.returncode, call.stderr)
        self.assertEqual("", call.stderr)
        transcript = json.loads(call.stdout)
        boundaries = transcript["boundaries"]

        self.assertEqual(
            "offline_temporary_canned_authority",
            transcript["mode"],
        )
        self.assertEqual(0, boundaries["provider_calls"])
        self.assertEqual(0, boundaries["credential_reads"])
        self.assertEqual(0, boundaries["repository_trip_writes"])
        self.assertFalse(boundaries["rendered"])
        self.assertFalse(boundaries["deployed"])
        self.assertFalse(boundaries["serialized_authority"])
        rendered = json.dumps(transcript, ensure_ascii=False, sort_keys=True)
        for private_value in (
            "private-phase533-acceptance-never-output",
            "schedule-alpha",
            "schedule-beta",
            "busan-arrival-terminal",
            "busan-dinner-location",
            "busan-day-3-activity-location",
            "hokkaido-stay-a",
            "09:00",
            "16:00",
            "18:00",
        ):
            self.assertNotIn(private_value, rendered)


if __name__ == "__main__":
    unittest.main()
