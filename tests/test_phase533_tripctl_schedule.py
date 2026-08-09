"""Phase 5.33 deterministic ``tripctl propose`` / ``score`` facade."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import trip_planner
import trip_planner.tripctl_schedule as tripctl_schedule_module
from tests.test_phase5_tripctl import _legacy_trip
from tests.test_phase533_tripctl_canonical import (
    PRIVATE,
    _canonical_plan,
    _canonical_trip,
    _tree_bytes,
)
from trip_planner.codec import build_plan, encode_plan
from trip_planner.tripctl import (
    TripctlError,
    proposal_failure,
    propose_trip,
    score_failure,
    score_trip,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "tripctl.py"
UTC = timezone.utc
EVALUATION_AT = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _verified_canonical_trip(root: Path) -> Path:
    trip_dir = _canonical_trip(root)
    plan = _canonical_plan()
    state = plan["state"]
    state["itinerary"]["days"][0]["places"][0][
        "evidence_state"
    ] = "verified"
    verified = build_plan(
        trip_id=plan["trip_id"],
        generation=2,
        state=state,
    )
    (trip_dir / "data" / "plan.json").write_bytes(encode_plan(verified))
    return trip_dir


class Phase533TripctlScheduleTests(unittest.TestCase):
    def test_propose_and_score_are_deterministic_redacted_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _verified_canonical_trip(Path(temporary))
            before = _tree_bytes(trip_dir)
            self.assertIs(trip_planner.propose_trip, propose_trip)
            self.assertIs(trip_planner.score_trip, score_trip)

            first_proposal = propose_trip(
                trip_dir,
                evaluation_at=EVALUATION_AT,
            )
            second_proposal = propose_trip(
                trip_dir / "data",
                evaluation_at=EVALUATION_AT,
            )
            proposal_ref = first_proposal["result"]["proposal_ref"]
            self.assertIsInstance(proposal_ref, str)
            first_score = score_trip(
                trip_dir,
                proposal_ref=proposal_ref,
                evaluation_at=EVALUATION_AT,
            )
            second_score = score_trip(
                trip_dir / "data",
                proposal_ref=proposal_ref,
                evaluation_at=EVALUATION_AT,
            )

            self.assertEqual(first_proposal, second_proposal)
            self.assertEqual(first_score, second_score)
            self.assertTrue(first_proposal["ok"])
            self.assertEqual("ready", first_proposal["status"])
            self.assertEqual("canonical", first_proposal["storage_mode"])
            self.assertEqual("solved", first_proposal["result"]["schedule_status"])
            self.assertEqual("score_proposal", first_proposal["next_action"])
            self.assertFalse(first_proposal["pending_review_retained"])
            self.assertTrue(first_proposal["result"]["provisional"])
            self.assertFalse(
                first_proposal["result"]["runtime_evidence_loaded"]
            )

            self.assertTrue(first_score["ok"])
            self.assertEqual("waiting_external", first_score["status"])
            self.assertEqual(proposal_ref, first_score["result"]["proposal_ref"])
            self.assertEqual("feasible", first_score["result"]["timeline_status"])
            self.assertEqual("refresh_evidence", first_score["next_action"])
            self.assertTrue(first_score["result"]["provisional"])
            self.assertFalse(first_score["pending_review_retained"])

            rendered = json.dumps(
                {"propose": first_proposal, "score": first_score},
                ensure_ascii=False,
                sort_keys=True,
            )
            for private_value in (
                PRIVATE,
                str(trip_dir),
                "activity-a",
                "day-1",
                "location-a",
                "09:15",
            ):
                self.assertNotIn(private_value, rendered)
            self.assertNotIn("assignments", rendered)
            self.assertNotIn("travel_ready", rendered)
            self.assertNotIn("apply", rendered)
            self.assertEqual(before, _tree_bytes(trip_dir))

    def test_missing_evidence_returns_no_partial_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _canonical_trip(Path(temporary))

            proposal = propose_trip(trip_dir, evaluation_at=EVALUATION_AT)

            self.assertTrue(proposal["ok"])
            self.assertEqual("waiting_external", proposal["status"])
            self.assertEqual("needs_evidence", proposal["result"]["schedule_status"])
            self.assertIsNone(proposal["result"]["proposal_ref"])
            self.assertEqual(0, proposal["result"]["candidate_count"])
            self.assertEqual("refresh_evidence", proposal["next_action"])
            self.assertIn(
                "SCHEDULE_NEEDS_EVIDENCE",
                {problem["code"] for problem in proposal["problems"]},
            )
            self.assertNotIn(PRIVATE, json.dumps(proposal))

    def test_score_rejects_wrong_ref_clock_and_canonical_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _verified_canonical_trip(Path(temporary))
            plan_path = trip_dir / "data" / "plan.json"
            proposal = propose_trip(trip_dir, evaluation_at=EVALUATION_AT)
            proposal_ref = proposal["result"]["proposal_ref"]

            with self.assertRaises(TripctlError) as malformed:
                score_trip(
                    trip_dir,
                    proposal_ref=PRIVATE,
                    evaluation_at=EVALUATION_AT,
                )
            self.assertEqual("INVALID_PROPOSAL_REF", malformed.exception.code)

            with self.assertRaises(TripctlError) as wrong_clock:
                score_trip(
                    trip_dir,
                    proposal_ref=proposal_ref,
                    evaluation_at=EVALUATION_AT + timedelta(seconds=1),
                )
            self.assertEqual("STALE_PROPOSAL_REF", wrong_clock.exception.code)
            rejection = score_failure(wrong_clock.exception)
            self.assertFalse(rejection["retryable"])
            self.assertIsNone(rejection["result"])
            self.assertEqual("rerun_proposal", rejection["next_action"])

            plan = _canonical_plan(generation=3)
            state = plan["state"]
            state["itinerary"]["days"][0]["places"][0][
                "evidence_state"
            ] = "verified"
            changed = build_plan(
                trip_id=plan["trip_id"],
                generation=3,
                state=state,
            )
            plan_path.write_bytes(encode_plan(changed))
            with self.assertRaises(TripctlError) as stale_revision:
                score_trip(
                    trip_dir,
                    proposal_ref=proposal_ref,
                    evaluation_at=EVALUATION_AT,
                )
            self.assertEqual("STALE_PROPOSAL_REF", stale_revision.exception.code)
            self.assertNotIn(
                PRIVATE,
                json.dumps(score_failure(stale_revision.exception)),
            )

    def test_inflight_source_drift_is_retryable_without_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _verified_canonical_trip(Path(temporary))
            plan_path = trip_dir / "data" / "plan.json"
            original_solve = tripctl_schedule_module.solve_schedule

            def solve_then_drift(problem: object) -> object:
                result = original_solve(problem)
                plan_path.write_bytes(plan_path.read_bytes() + b" ")
                return result

            with patch(
                "trip_planner.tripctl_schedule.solve_schedule",
                side_effect=solve_then_drift,
            ):
                with self.assertRaises(TripctlError) as raised:
                    propose_trip(trip_dir, evaluation_at=EVALUATION_AT)

            self.assertEqual("STALE_CANONICAL_PLAN", raised.exception.code)
            failure = proposal_failure(raised.exception)
            self.assertTrue(failure["retryable"])
            self.assertIsNone(failure["result"])
            self.assertEqual("retry_proposal", failure["next_action"])
            self.assertNotIn(PRIVATE, json.dumps(failure))

    def test_legacy_mode_is_rejected_before_schedule_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _legacy_trip(Path(temporary))
            with patch(
                "trip_planner.tripctl.propose_canonical_schedule",
                side_effect=AssertionError("scheduler must not run"),
            ):
                with self.assertRaises(TripctlError) as proposal_error:
                    propose_trip(trip_dir, evaluation_at=EVALUATION_AT)
            self.assertEqual("CANONICAL_PLAN_REQUIRED", proposal_error.exception.code)
            proposal_rejection = proposal_failure(proposal_error.exception)
            self.assertEqual("legacy", proposal_rejection["storage_mode"])
            self.assertEqual("use_canonical_trip", proposal_rejection["next_action"])

            with patch(
                "trip_planner.tripctl.score_canonical_schedule",
                side_effect=AssertionError("scorer must not run"),
            ):
                with self.assertRaises(TripctlError) as score_error:
                    score_trip(
                        trip_dir,
                        proposal_ref="sha256:" + "0" * 64,
                        evaluation_at=EVALUATION_AT,
                    )
            self.assertEqual("CANONICAL_PLAN_REQUIRED", score_error.exception.code)

    def test_cli_propose_score_chain_and_errors_are_json_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _verified_canonical_trip(Path(temporary))
            evaluation_text = EVALUATION_AT.isoformat()
            proposal_call = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "propose",
                    str(trip_dir),
                    "--evaluation-at",
                    evaluation_text,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, proposal_call.returncode, proposal_call.stderr)
            self.assertEqual("", proposal_call.stderr)
            proposal = json.loads(proposal_call.stdout)
            proposal_ref = proposal["result"]["proposal_ref"]

            score_call = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "score",
                    str(trip_dir),
                    "--proposal-ref",
                    proposal_ref,
                    "--evaluation-at",
                    evaluation_text,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, score_call.returncode, score_call.stderr)
            self.assertEqual("", score_call.stderr)
            self.assertEqual(
                proposal_ref,
                json.loads(score_call.stdout)["result"]["proposal_ref"],
            )
            self.assertNotIn(PRIVATE, proposal_call.stdout + score_call.stdout)

            invalid_clock = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "propose",
                    str(trip_dir),
                    "--evaluation-at",
                    PRIVATE,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, invalid_clock.returncode)
            self.assertEqual("", invalid_clock.stdout)
            rejection = json.loads(invalid_clock.stderr)
            self.assertEqual(
                "INVALID_EVALUATION_AT",
                rejection["problems"][0]["code"],
            )
            self.assertNotIn(PRIVATE, invalid_clock.stderr)

            invalid_ref = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "score",
                    str(trip_dir),
                    "--proposal-ref",
                    PRIVATE,
                    "--evaluation-at",
                    evaluation_text,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, invalid_ref.returncode)
            self.assertEqual("", invalid_ref.stdout)
            self.assertEqual(
                "INVALID_PROPOSAL_REF",
                json.loads(invalid_ref.stderr)["problems"][0]["code"],
            )
            self.assertNotIn(PRIVATE, invalid_ref.stderr)


if __name__ == "__main__":
    unittest.main()
