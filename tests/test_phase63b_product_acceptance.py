"""Offline Phase 6.3B Busan/Hokkaido product acceptance."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.phase63b_acceptance import (
    ACCEPTANCE_VERSION,
    run_walkthrough,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "phase63b_acceptance.py"


class Phase63BProductAcceptanceTests(unittest.TestCase):
    def test_both_semantic_families_complete_phase6_acceptance(self) -> None:
        transcript = run_walkthrough()
        busan = transcript["busan"]
        hokkaido = transcript["hokkaido"]

        self.assertEqual(ACCEPTANCE_VERSION, transcript["contract_version"])
        for case, identity_count, route_count in (
            (busan, 1, 2),
            (hokkaido, 2, 1),
        ):
            self.assertEqual(
                {
                    "draft": "draft",
                    "review": "review",
                    "travel_ready": "travel_ready",
                    "private_delivery": "created",
                },
                case["status_chain"],
            )
            self.assertTrue(case["phase533_typed_review_completed"])
            self.assertTrue(case["post_apply_review_reconfirmed"])
            self.assertTrue(case["lodging_identity_alone_stayed_review"])
            self.assertTrue(
                case["identity_only_ready_bundle_rejected_prewrite"]
            )
            self.assertEqual(identity_count, case["identity_evidence_count"])
            self.assertEqual(route_count, case["route_evidence_count"])
            self.assertEqual(3, case["used_evidence_count"])
            self.assertEqual(0, case["problem_count"])
            self.assertTrue(case["recheck_deadline_bound"])
            self.assertTrue(case["persisted_lodging_evidence_unverified"])
            self.assertTrue(case["canonical_bytes_unchanged_by_delivery"])
            self.assertTrue(case["durable_evidence_unchanged_by_delivery"])
            self.assertTrue(case["memory_only_routes_not_persisted"])
            self.assertTrue(case["write_review_reloaded_source"])
            self.assertTrue(case["capture_performed_zero_writes"])
            self.assertTrue(case["matching_write_response_consumed"])
            self.assertTrue(case["exact_private_tree_confirmed"])
            self.assertEqual(3, case["private_artifact_count"])
            self.assertTrue(case["manifest_commit_present"])
            self.assertTrue(all(case["phase533_semantics"].values()))

        busan_semantics = busan["phase533_semantics"]
        for name in (
            "arrival_boundary_preserved",
            "dinner_boundary_preserved",
            "daily_lodging_anchors_preserved",
            "semantic_locations_distinct",
            "migration_protection_preserved",
            "approval_resume_confirmed",
        ):
            self.assertTrue(busan_semantics[name])

        hokkaido_semantics = hokkaido["phase533_semantics"]
        for name in (
            "split_stays_contiguous",
            "split_stay_anchor_preserved",
            "checkin_boundary_preserved",
            "cross_city_duration_preserved",
            "winter_buffer_preserved",
            "lost_ack_receipt_reconciled",
        ):
            self.assertTrue(hokkaido_semantics[name])

    def test_cli_transcript_is_redacted_and_truthful_about_temp_writes(
        self,
    ) -> None:
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
        for name in (
            "real_provider_calls",
            "credential_reads",
            "repository_trip_reads",
            "repository_trip_writes",
        ):
            self.assertEqual(0, boundaries[name])
        self.assertEqual(2, boundaries["temporary_canonical_stores"])
        self.assertEqual(2, boundaries["temporary_durable_evidence_stores"])
        self.assertEqual(2, boundaries["temporary_memory_evidence_sessions"])
        self.assertEqual(
            2,
            boundaries["temporary_ready_bundle_projection_sets"],
        )
        self.assertEqual(2, boundaries["temporary_private_bundles"])
        for name in (
            "browser_opened",
            "calendar_imported",
            "served_or_shared",
            "public_source_created",
            "deployed",
            "serialized_authority",
            "temporary_artifacts_retained",
        ):
            self.assertFalse(boundaries[name])

        rendered = json.dumps(transcript, ensure_ascii=False, sort_keys=True)
        for private_value in (
            "phase63b-private",
            "private-phase533-acceptance-never-output",
            "lodging-location-",
            "schedule-alpha",
            "schedule-beta",
            "busan-arrival-terminal",
            "busan-dinner-location",
            "busan-day-3-activity-location",
            "hokkaido-stay-a",
            "hokkaido-stay-b",
            "phase63b-canned-route",
            "Phase 6.3B canned route",
            "ChIJ",
            "observation_id",
            "snapshot_id",
            "readiness_id",
            "/tmp/",
            "09:00",
            "10:00",
            "16:00",
            "18:00",
        ):
            self.assertNotIn(private_value, rendered)
        self.assertNotRegex(rendered, r"\b[0-9a-f]{64}\b")


if __name__ == "__main__":
    unittest.main()
