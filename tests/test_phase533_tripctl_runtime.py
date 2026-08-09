"""Phase 5.33 process-local runtime evidence/readiness facade."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import trip_planner
import trip_planner.tripctl_schedule as tripctl_schedule_module
from tests.test_phase4_composition import _policies, _snapshot
from tests.test_phase533_tripctl_canonical import (
    PRIVATE,
    _canonical_trip,
    _tree_bytes,
)
from tests.test_phase533_tripctl_schedule import (
    EVALUATION_AT,
    _verified_canonical_trip,
)
from trip_planner import EvidenceLedger
from trip_planner.lodging import (
    LodgingRequirement,
    assess_lodging_intake,
)
from trip_planner.tripctl import (
    TripctlError,
    command_failure,
    propose_trip,
    propose_trip_with_evidence,
    score_trip,
    score_trip_with_evidence,
    validate_trip_with_evidence,
)


def _runtime_inputs():
    ledger = EvidenceLedger(_policies("unused-provider"))
    snapshot = _snapshot(ledger, evaluation_at=EVALUATION_AT)
    lodging_intake = assess_lodging_intake(
        stay_start=date(2026, 10, 1),
        stay_end=date(2026, 10, 2),
        requirement=LodgingRequirement.NOT_REQUIRED,
    )
    return ledger, snapshot, lodging_intake


class Phase533TripctlRuntimeTests(unittest.TestCase):
    def test_runtime_validate_propose_score_are_redacted_and_read_only(
        self,
    ) -> None:
        _ledger, snapshot, lodging_intake = _runtime_inputs()
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _verified_canonical_trip(Path(temporary))
            before = _tree_bytes(trip_dir)

            validation = validate_trip_with_evidence(
                trip_dir,
                evidence_snapshot=snapshot,
                lodging_intake=lodging_intake,
            )
            validation_replay = validate_trip_with_evidence(
                trip_dir / "data",
                evidence_snapshot=snapshot,
                lodging_intake=lodging_intake,
            )
            proposal = propose_trip_with_evidence(
                trip_dir,
                evidence_snapshot=snapshot,
                lodging_intake=lodging_intake,
            )
            proposal_replay = propose_trip_with_evidence(
                trip_dir / "data",
                evidence_snapshot=snapshot,
                lodging_intake=lodging_intake,
            )
            proposal_ref = proposal["result"]["proposal_ref"]
            score = score_trip_with_evidence(
                trip_dir,
                proposal_ref=proposal_ref,
                evidence_snapshot=snapshot,
                lodging_intake=lodging_intake,
            )
            score_replay = score_trip_with_evidence(
                trip_dir / "data",
                proposal_ref=proposal_ref,
                evidence_snapshot=snapshot,
                lodging_intake=lodging_intake,
            )

            self.assertIs(
                trip_planner.validate_trip_with_evidence,
                validate_trip_with_evidence,
            )
            self.assertIs(
                trip_planner.propose_trip_with_evidence,
                propose_trip_with_evidence,
            )
            self.assertIs(
                trip_planner.score_trip_with_evidence,
                score_trip_with_evidence,
            )
            self.assertEqual(validation, validation_replay)
            self.assertEqual(proposal, proposal_replay)
            self.assertEqual(score, score_replay)
            self.assertEqual("travel_ready", validation["status"])
            self.assertEqual("none", validation["next_action"])
            self.assertEqual([], validation["problems"])
            self.assertTrue(
                validation["result"]["runtime_evidence_loaded"]
            )
            self.assertEqual(
                "canonical",
                validation["result"]["readiness_scope"],
            )
            self.assertFalse(validation["result"]["apply_authority"])
            self.assertFalse(
                validation["result"]["canonical_write_performed"]
            )

            self.assertEqual("ready", proposal["status"])
            self.assertEqual("solved", proposal["result"]["schedule_status"])
            self.assertTrue(proposal["result"]["runtime_evidence_loaded"])
            self.assertFalse(proposal["result"]["provisional"])
            self.assertEqual(
                "canonical_base",
                proposal["result"]["readiness_scope"],
            )
            self.assertEqual(
                "travel_ready",
                proposal["result"]["readiness"]["status"],
            )
            self.assertIsInstance(proposal_ref, str)

            self.assertEqual("review_required", score["status"])
            self.assertEqual("review_proposal", score["next_action"])
            self.assertTrue(score["requires_user_review"])
            self.assertTrue(score["result"]["runtime_evidence_loaded"])
            self.assertFalse(score["result"]["provisional"])
            self.assertFalse(score["result"]["apply_authority"])
            self.assertFalse(score["result"]["canonical_write_performed"])
            self.assertEqual(proposal_ref, score["result"]["proposal_ref"])
            self.assertEqual(
                "canonical_base",
                score["result"]["readiness_scope"],
            )
            self.assertEqual(
                proposal["result"]["evidence_binding_ref"],
                score["result"]["evidence_binding_ref"],
            )
            self.assertEqual(
                validation["result"]["runtime_context_ref"],
                proposal["result"]["runtime_context_ref"],
            )
            self.assertEqual(
                proposal["result"]["runtime_context_ref"],
                score["result"]["runtime_context_ref"],
            )

            rendered = json.dumps(
                {
                    "validation": validation,
                    "proposal": proposal,
                    "score": score,
                },
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
            self.assertNotIn('"trip_id"', rendered)
            self.assertNotIn("assignments", rendered)
            self.assertEqual(before, _tree_bytes(trip_dir))

    def test_snapshot_clock_or_binding_drift_invalidates_proposal_ref(self) -> None:
        ledger, snapshot, lodging_intake = _runtime_inputs()
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _verified_canonical_trip(Path(temporary))
            proposal = propose_trip_with_evidence(
                trip_dir,
                evidence_snapshot=snapshot,
                lodging_intake=lodging_intake,
            )
            proposal_ref = proposal["result"]["proposal_ref"]

            with self.assertRaises(TripctlError) as readiness_drift:
                score_trip_with_evidence(
                    trip_dir,
                    proposal_ref=proposal_ref,
                    evidence_snapshot=snapshot,
                )
            self.assertEqual(
                "STALE_PROPOSAL_REF",
                readiness_drift.exception.code,
            )

            later = _snapshot(
                ledger,
                evaluation_at=EVALUATION_AT + timedelta(seconds=1),
            )
            with self.assertRaises(TripctlError) as clock_drift:
                score_trip_with_evidence(
                    trip_dir,
                    proposal_ref=proposal_ref,
                    evidence_snapshot=later,
                    lodging_intake=lodging_intake,
                )
            self.assertEqual("STALE_PROPOSAL_REF", clock_drift.exception.code)

            changed_ledger = EvidenceLedger(_policies("different-provider"))
            changed = _snapshot(
                changed_ledger,
                evaluation_at=EVALUATION_AT,
            )
            with self.assertRaises(TripctlError) as binding_drift:
                score_trip_with_evidence(
                    trip_dir,
                    proposal_ref=proposal_ref,
                    evidence_snapshot=changed,
                    lodging_intake=lodging_intake,
                )
            self.assertEqual(
                "STALE_PROPOSAL_REF",
                binding_drift.exception.code,
            )

    def test_exact_but_insufficient_snapshot_does_not_fake_readiness(self) -> None:
        _ledger, snapshot, lodging_intake = _runtime_inputs()
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _canonical_trip(Path(temporary))

            validation = validate_trip_with_evidence(
                trip_dir,
                evidence_snapshot=snapshot,
                lodging_intake=lodging_intake,
            )
            proposal = propose_trip_with_evidence(
                trip_dir,
                evidence_snapshot=snapshot,
                lodging_intake=lodging_intake,
            )

            self.assertEqual("review", validation["status"])
            self.assertNotEqual("travel_ready", validation["status"])
            self.assertEqual("waiting_external", proposal["status"])
            self.assertEqual(
                "needs_evidence",
                proposal["result"]["schedule_status"],
            )
            self.assertIsNone(proposal["result"]["proposal_ref"])
            self.assertEqual(0, proposal["result"]["candidate_count"])
            self.assertTrue(proposal["result"]["runtime_evidence_loaded"])
            self.assertIn(
                "SCHEDULE_NEEDS_EVIDENCE",
                {item["code"] for item in proposal["problems"]},
            )
            self.assertNotIn("travel_ready", json.dumps(proposal))

    def test_disk_only_and_evidence_bound_refs_are_not_interchangeable(
        self,
    ) -> None:
        _ledger, snapshot, lodging_intake = _runtime_inputs()
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _verified_canonical_trip(Path(temporary))
            disk = propose_trip(
                trip_dir,
                evaluation_at=EVALUATION_AT,
            )
            runtime = propose_trip_with_evidence(
                trip_dir,
                evidence_snapshot=snapshot,
                lodging_intake=lodging_intake,
            )
            disk_ref = disk["result"]["proposal_ref"]
            runtime_ref = runtime["result"]["proposal_ref"]

            self.assertNotEqual(disk_ref, runtime_ref)
            with self.assertRaises(TripctlError) as disk_rejects_runtime:
                score_trip(
                    trip_dir,
                    proposal_ref=runtime_ref,
                    evaluation_at=EVALUATION_AT,
                )
            self.assertEqual(
                "STALE_PROPOSAL_REF",
                disk_rejects_runtime.exception.code,
            )
            with self.assertRaises(TripctlError) as runtime_rejects_disk:
                score_trip_with_evidence(
                    trip_dir,
                    proposal_ref=disk_ref,
                    evidence_snapshot=snapshot,
                    lodging_intake=lodging_intake,
                )
            self.assertEqual(
                "STALE_PROPOSAL_REF",
                runtime_rejects_disk.exception.code,
            )

    def test_serialized_evidence_claim_is_rejected_before_solver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _verified_canonical_trip(Path(temporary))
            with patch(
                "trip_planner.tripctl_schedule.solve_schedule",
                side_effect=AssertionError("solver must not run"),
            ):
                with self.assertRaises(TripctlError) as raised:
                    propose_trip_with_evidence(
                        trip_dir,
                        evidence_snapshot={"snapshot_id": "claimed"},
                    )

            self.assertEqual("INVALID_RUNTIME_EVIDENCE", raised.exception.code)
            failure = command_failure("propose", raised.exception)
            self.assertEqual("canonical", failure["storage_mode"])
            self.assertEqual("use_developer_runtime", failure["next_action"])
            self.assertNotIn("claimed", json.dumps(failure))

    def test_inflight_canonical_drift_has_no_partial_runtime_proposal(self) -> None:
        _ledger, snapshot, lodging_intake = _runtime_inputs()
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
                    propose_trip_with_evidence(
                        trip_dir,
                        evidence_snapshot=snapshot,
                        lodging_intake=lodging_intake,
                    )

            self.assertEqual("STALE_CANONICAL_PLAN", raised.exception.code)
            failure = command_failure("propose", raised.exception)
            self.assertTrue(failure["retryable"])
            self.assertIsNone(failure["result"])
            self.assertEqual("retry_proposal", failure["next_action"])
            self.assertNotIn(PRIVATE, json.dumps(failure))


if __name__ == "__main__":
    unittest.main()
