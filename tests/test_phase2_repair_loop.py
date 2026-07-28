"""Adversarial offline tests for the bounded Phase 2 repair controller."""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from trip_planner.codec import build_plan, compute_revision, encode_plan
from trip_planner.facts import (
    EvidenceLedger,
    EvidencePersistence,
    EvidenceSnapshot,
    FactKind,
    ProviderPolicy,
    ProviderPolicyRegistry,
)
from trip_planner.models import CheckStatus
from trip_planner.mutations import (
    AddActivity,
    ApprovalGrant,
    PlanPatch,
    RemoveActivity,
    UpdateActivity,
)
from trip_planner.repair import OperationReason, ProposalIntent, RepairBudget
from trip_planner.repair_loop import (
    HumanCheckpointGrant,
    RepairController,
    RepairState,
)
from trip_planner.store import StoreProblem, StoreResult, TripStore


_EVALUATION_AT = datetime(
    2026,
    7,
    27,
    9,
    30,
    tzinfo=timezone(timedelta(hours=8)),
)
_EVALUATION_AT_UTC = datetime(2026, 7, 27, 1, 30, tzinfo=timezone.utc)
_DELEGATE = object()
_EVIDENCE_POLICIES = ProviderPolicyRegistry(
    policies=(
        ProviderPolicy(
            policy_id="repair-routes-v1",
            provider_id="repair-routes",
            adapter_id="repair-routes",
            adapter_version="v1",
            contract_region="test",
            allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
            allowed_value_fields=("duration_min", "mode"),
            allowed_operations=("compute-route",),
            persistence=EvidencePersistence.MEMORY_ONLY,
            max_validity_seconds=24 * 60 * 60,
            max_retention_seconds=24 * 60 * 60,
        ),
    )
)


def _problem_codes(value: Any) -> set[str]:
    return {problem.code for problem in value.problems}


class _EvidenceResult:
    def __init__(self, snapshot: EvidenceSnapshot) -> None:
        self.value = snapshot

    def snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        if evaluation_at != self.value.evaluation_at:
            raise AssertionError("controller changed the evidence evaluation time")
        return self.value


class _MutableEvidenceSource:
    def __init__(
        self,
        generations: Sequence[int] = (0,),
        *,
        fail_on_load: int | None = None,
    ) -> None:
        self.generations = list(generations)
        self.generation = self.generations[-1]
        self.load_calls = 0
        self.fail_on_load = fail_on_load

    def load(self) -> _EvidenceResult:
        index = min(self.load_calls, len(self.generations) - 1)
        generation = self.generations[index]
        self.load_calls += 1
        if self.load_calls == self.fail_on_load:
            raise RuntimeError("provider-secret-error-marker")
        ledger = EvidenceLedger(
            _EVIDENCE_POLICIES,
            generation=generation,
        )
        return _EvidenceResult(
            EvidenceSnapshot.from_ledger(
                ledger,
                evaluation_at=_EVALUATION_AT_UTC,
                purge_now=_EVALUATION_AT_UTC,
            )
        )

    def advance(self, generation: int) -> None:
        self.generations = [generation]
        self.generation = generation


class _RecordingRepository:
    """Transparent TripStore adapter that records controller calls."""

    def __init__(self, store: TripStore) -> None:
        self.store = store
        self.preview_calls: list[tuple[Any, tuple[Any, ...], datetime | None]] = []
        self.apply_calls: list[tuple[Any, tuple[Any, ...], datetime | None]] = []

    def load_plan(self) -> dict[str, Any]:
        return self.store.load_plan()

    def preview_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        self.preview_calls.append((patch, tuple(approvals), evaluation_at))
        return self.store.preview_patch(
            patch,
            approvals,
            evaluation_at=evaluation_at,
        )

    def apply_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        dry_run: bool = False,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        self.apply_calls.append((patch, tuple(approvals), evaluation_at))
        return self.store.apply_patch(
            patch,
            approvals,
            dry_run=dry_run,
            evaluation_at=evaluation_at,
        )


class _ScriptedApplyRepository(_RecordingRepository):
    """Return injected apply outcomes before optionally delegating to TripStore."""

    def __init__(
        self,
        store: TripStore,
        outcomes: Sequence[StoreResult | object],
    ) -> None:
        super().__init__(store)
        self.outcomes = list(outcomes)

    def apply_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        dry_run: bool = False,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        self.apply_calls.append((patch, tuple(approvals), evaluation_at))
        if not self.outcomes:
            raise AssertionError("controller made an unexpected extra apply call")
        outcome = self.outcomes.pop(0)
        if outcome is _DELEGATE:
            return self.store.apply_patch(
                patch,
                approvals,
                dry_run=dry_run,
                evaluation_at=evaluation_at,
            )
        assert isinstance(outcome, StoreResult)
        return outcome


class _NondeterministicPreviewRepository(_RecordingRepository):
    """Change a candidate after its initial review without changing its draft."""

    def preview_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        result = super().preview_patch(
            patch,
            approvals,
            evaluation_at=evaluation_at,
        )
        if len(self.preview_calls) != 2:
            return result
        candidate = result.mutable_candidate_plan()
        if candidate is None:
            raise AssertionError("expected a canonical preview candidate")
        candidate["state"]["trip"]["title"] = "Changed during re-preview"
        candidate["revision"] = compute_revision(candidate)
        return replace(
            result,
            applied_revision=candidate["revision"],
            candidate_plan=candidate,
        )


class _ExplodingPreviewRepository(_RecordingRepository):
    def preview_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        self.preview_calls.append((patch, tuple(approvals), evaluation_at))
        raise RuntimeError("provider\nfailure " + ("x" * 5000))


class _ExternalWinnerRepository(_RecordingRepository):
    """Let another patch win between controller preview and apply."""

    def apply_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        dry_run: bool = False,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        self.apply_calls.append((patch, tuple(approvals), evaluation_at))
        current = self.store.load_plan()
        external = PlanPatch(
            trip_id=current["trip_id"],
            base_revision=current["revision"],
            idempotency_key="external-cas-winner",
            operations=(
                UpdateActivity(
                    "external-note",
                    "activity-alpha",
                    {"note": "external writer won"},
                ),
            ),
            intent="simulate a concurrent writer",
        )
        won = self.store.apply_patch(
            external,
            evaluation_at=evaluation_at,
        )
        if not won.success:
            raise AssertionError(won.to_dict())
        return self.store.apply_patch(
            patch,
            approvals,
            dry_run=dry_run,
            evaluation_at=evaluation_at,
        )


class _PostCommitReadFailureRepository(_RecordingRepository):
    def __init__(self, store: TripStore) -> None:
        super().__init__(store)
        self.fail_reads = False

    def load_plan(self) -> dict[str, Any]:
        if self.fail_reads:
            raise RuntimeError("post-commit\nread unavailable")
        return super().load_plan()

    def apply_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        dry_run: bool = False,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        result = super().apply_patch(
            patch,
            approvals,
            dry_run=dry_run,
            evaluation_at=evaluation_at,
        )
        self.fail_reads = True
        return result


class Phase2RepairLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.trips_root = self.root / "trips"
        self.slug = "repair-loop-fixture"
        self.data_dir = self.trips_root / self.slug / "data"
        self.data_dir.mkdir(parents=True)
        self.plan_path = self.data_dir / "plan.json"
        self.activity_id = "activity-alpha"
        self.day_id = "day-one"
        self.location_id = "location-alpha"
        self._write_plan(self._plan())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _plan(
        self,
        *,
        decision_state: str = "selected",
        flexibility: str = "movable",
        activities: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if activities is None:
            activities = [
                self._activity(
                    self.activity_id,
                    decision_state=decision_state,
                    flexibility=flexibility,
                )
            ]
        return build_plan(
            trip_id=self.slug,
            generation=1,
            state={
                "trip": {
                    "slug": self.slug,
                    "title": "Repair loop fixture",
                    "subtitle": "offline",
                    "timezone": "Asia/Taipei",
                    "date_range": "2026-10-01 ~ 2026-10-01",
                    "cities": ["Fixture City"],
                    "constraints": [],
                },
                "itinerary": {
                    "available_modes": ["walking"],
                    "days": [
                        {
                            "day_id": self.day_id,
                            "day": 1,
                            "date": "2026-10-01",
                            "title": "Fixture day",
                            "timezone": "Asia/Taipei",
                            "available_start": "08:00",
                            "available_end": "20:00",
                            "start_location_id": self.location_id,
                            "end_location_id": self.location_id,
                            "allowed_modes": ["walking"],
                            "places": activities,
                            "travel": [],
                        }
                    ],
                },
            },
        )

    def _activity(
        self,
        activity_id: str,
        *,
        time: str = "09:00",
        duration_min: int | None = None,
        decision_state: str = "selected",
        flexibility: str = "movable",
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "activity_id": activity_id,
            "title": activity_id.replace("-", " ").title(),
            "location_id": self.location_id,
            "time": time,
            "decision_state": decision_state,
            "flexibility": flexibility,
            "evidence_state": "verified",
            "type": "activity",
            "note": "",
        }
        if duration_min is not None:
            result["duration_min"] = duration_min
        return result

    def _write_plan(self, plan: dict[str, Any]) -> None:
        self.plan_path.write_bytes(encode_plan(plan))

    def _store(self) -> TripStore:
        return TripStore(self.trips_root, self.slug)

    def _controller(
        self,
        repository: Any | None = None,
        *,
        budget: RepairBudget | None = None,
        evidence_source: Any | None = None,
    ) -> RepairController:
        return RepairController(
            repository or self._store(),
            run_id="repair-run-001",
            evaluation_at=_EVALUATION_AT,
            budget=budget,
            evidence_source=evidence_source,
        )

    def _issue_id(
        self,
        snapshot: Any,
        code: str,
        *,
        activity_id: str | None = None,
    ) -> str:
        matches = [
            issue
            for issue in snapshot.issues
            if issue.check.code == code
            and (
                activity_id is None
                or activity_id in issue.check.activity_ids
            )
        ]
        self.assertEqual(
            1,
            len(matches),
            (
                f"expected one {code} issue for {activity_id!r}; got "
                f"{[(issue.check.code, issue.check.activity_ids) for issue in snapshot.issues]}"
            ),
        )
        return matches[0].issue_id

    def _proposal(
        self,
        snapshot: Any,
        *operations: Any,
        issue_code: str = "MISSING_DURATION",
        issue_activity_id: str | None = None,
        option_keys: Sequence[str] | None = None,
    ) -> ProposalIntent:
        issue_id = self._issue_id(
            snapshot,
            issue_code,
            activity_id=issue_activity_id or self.activity_id,
        )
        if option_keys is None:
            option_keys = ("set_activity_duration",) * len(operations)
        self.assertEqual(len(operations), len(option_keys))
        return ProposalIntent(
            operations=tuple(operations),
            reasons=tuple(
                OperationReason(
                    op_id=operation.op_id,
                    issue_ids=(issue_id,),
                    option_key=option_key,
                    reason=f"Repair {issue_code} with {operation.op_id}.",
                )
                for operation, option_key in zip(
                    operations,
                    option_keys,
                    strict=True,
                )
            ),
            summary=f"Repair {issue_code}.",
        )

    def _human_grant(self, review_id: str) -> HumanCheckpointGrant:
        return HumanCheckpointGrant(
            review_id=review_id,
            approved_by="fixture-human",
            approved_at=_EVALUATION_AT,
        )

    def _store_grant(self, scope: str) -> ApprovalGrant:
        return ApprovalGrant(
            approval_id="approval-fixture-001",
            scope_digest=scope,
            approved_by="fixture-human",
            approved_at="2026-07-27T01:30:00+00:00",
        )

    def _safe_duration_review(
        self,
        controller: RepairController,
        snapshot: Any | None = None,
    ) -> Any:
        snapshot = snapshot or controller.inspect()
        proposal = self._proposal(
            snapshot,
            UpdateActivity(
                "model-duration",
                self.activity_id,
                {"duration_min": 45},
            ),
        )
        return controller.submit(snapshot.snapshot_id, proposal)

    def test_inspect_uses_fixed_utc_evaluation_context_and_reports_ready(self) -> None:
        controller = self._controller(
            budget=RepairBudget(
                max_iterations=3,
                max_provider_calls=2,
                max_changes=5,
                max_auto_changes_per_patch=5,
            )
        )

        first = controller.inspect()
        second = controller.inspect()

        self.assertEqual(_EVALUATION_AT_UTC, first.evaluation_at)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, first.report.status)
        self.assertEqual(RepairState.READY, controller.state)
        self.assertEqual(3, first.remaining_iterations)
        self.assertEqual(2, first.remaining_provider_calls)
        self.assertEqual(5, first.remaining_changes)
        self.assertIn(
            "MISSING_DURATION",
            {issue.check.code for issue in first.issues},
        )

    def test_submit_reports_evidence_revision_drift(self) -> None:
        source = _MutableEvidenceSource()
        controller = self._controller(evidence_source=source)
        snapshot = controller.inspect()
        proposal = self._proposal(
            snapshot,
            UpdateActivity(
                "model-duration",
                self.activity_id,
                {"duration_min": 45},
            ),
        )

        source.advance(1)
        result = controller.submit(snapshot.snapshot_id, proposal)

        self.assertEqual(
            {"EVIDENCE_REVISION_CHANGED"},
            _problem_codes(result),
        )
        self.assertEqual(snapshot.state_digest, result.snapshot.state_digest)
        self.assertNotEqual(
            snapshot.decision_context_digest,
            result.snapshot.decision_context_digest,
        )

    def test_submit_redacts_evidence_source_exception_text(self) -> None:
        source = _MutableEvidenceSource(fail_on_load=2)
        controller = self._controller(evidence_source=source)
        snapshot = controller.inspect()
        proposal = self._proposal(
            snapshot,
            UpdateActivity(
                "model-duration",
                self.activity_id,
                {"duration_min": 45},
            ),
        )

        result = controller.submit(snapshot.snapshot_id, proposal)

        self.assertEqual(
            {"EVIDENCE_SOURCE_FAILED"},
            _problem_codes(result),
        )
        self.assertEqual(RepairState.WAITING_EXTERNAL, result.state)
        self.assertNotIn(
            "provider-secret-error-marker",
            str(result.to_dict()),
        )

    def test_same_effect_can_retry_under_new_evidence_context(self) -> None:
        source = _MutableEvidenceSource()
        controller = self._controller(evidence_source=source)
        snapshot = controller.inspect()
        proposal = self._proposal(
            snapshot,
            UpdateActivity(
                "model-note",
                self.activity_id,
                {"note": "Does not repair duration"},
            ),
        )

        first = controller.submit(snapshot.snapshot_id, proposal)
        self.assertIn("TARGET_ISSUE_PERSISTS", _problem_codes(first))

        source.advance(1)
        refreshed = controller.inspect()
        second = controller.submit(refreshed.snapshot_id, proposal)

        self.assertNotIn("REPEATED_ATTEMPT", _problem_codes(second))
        self.assertIn("TARGET_ISSUE_PERSISTS", _problem_codes(second))

    def test_evidence_refresh_clears_pending_without_oscillation(self) -> None:
        source = _MutableEvidenceSource()
        controller = self._controller(evidence_source=source)
        review = self._safe_duration_review(controller)

        source.advance(1)
        refreshed = controller.inspect()
        stale_review = controller.commit(review.review_id or "")
        self.assertIn("UNKNOWN_REVIEW", _problem_codes(stale_review))
        self.assertNotEqual(RepairState.STOPPED, controller.state)

        source.advance(0)
        restored_evidence = controller.inspect()
        self.assertEqual(
            refreshed.state_digest,
            restored_evidence.state_digest,
        )
        self.assertNotEqual(RepairState.STOPPED, controller.state)

    def test_candidate_preview_reuses_the_pinned_evidence_snapshot(self) -> None:
        source = _MutableEvidenceSource()
        controller = self._controller(evidence_source=source)

        review = self._safe_duration_review(controller)

        self.assertEqual(2, source.load_calls)
        self.assertIsNotNone(review.candidate_snapshot)
        self.assertEqual(
            review.snapshot.evidence_binding,
            review.candidate_snapshot.evidence_binding,
        )

    def test_commit_rejects_evidence_revision_drift_before_apply(self) -> None:
        source = _MutableEvidenceSource()
        repository = _RecordingRepository(self._store())
        controller = self._controller(
            repository,
            evidence_source=source,
        )
        review = self._safe_duration_review(controller)

        source.advance(1)
        result = controller.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertEqual(
            {"EVIDENCE_REVISION_CHANGED"},
            _problem_codes(result),
        )
        self.assertEqual(0, len(repository.apply_calls))

    def test_post_commit_evidence_drift_preserves_applied_result(self) -> None:
        source = _MutableEvidenceSource((0, 0, 0, 0, 1))
        repository = _RecordingRepository(self._store())
        controller = self._controller(
            repository,
            evidence_source=source,
        )
        review = self._safe_duration_review(controller)

        result = controller.commit(review.review_id or "")

        self.assertTrue(result.applied, result.problems)
        self.assertEqual(RepairState.WAITING_EXTERNAL, result.state)
        self.assertIn(
            "EVIDENCE_REVISION_CHANGED",
            _problem_codes(result),
        )
        self.assertEqual(1, len(repository.apply_calls))
        persisted = repository.load_plan()["state"]["itinerary"]["days"][0][
            "places"
        ][0]
        self.assertEqual(45, persisted["duration_min"])

    def test_post_commit_evidence_failure_is_applied_and_waiting(self) -> None:
        source = _MutableEvidenceSource(
            (0, 0, 0, 0, 0),
            fail_on_load=5,
        )
        repository = _RecordingRepository(self._store())
        controller = self._controller(
            repository,
            evidence_source=source,
        )
        review = self._safe_duration_review(controller)

        result = controller.commit(review.review_id or "")

        self.assertTrue(result.applied, result.problems)
        self.assertEqual(RepairState.WAITING_EXTERNAL, result.state)
        self.assertEqual(
            {"EVIDENCE_SOURCE_FAILED"},
            _problem_codes(result),
        )
        serialized = str(result.to_dict())
        self.assertNotIn("provider-secret-error-marker", serialized)
        persisted = repository.load_plan()["state"]["itinerary"]["days"][0][
            "places"
        ][0]
        self.assertEqual(45, persisted["duration_min"])

    def test_malformed_and_stale_proposals_each_consume_iteration_budget(self) -> None:
        controller = self._controller(
            budget=RepairBudget(
                max_iterations=2,
                max_provider_calls=0,
                max_changes=4,
                max_auto_changes_per_patch=4,
            )
        )
        snapshot = controller.inspect()

        malformed = controller.submit(snapshot.snapshot_id, "{not-json")
        self.assertIn("INVALID_JSON", _problem_codes(malformed))
        self.assertEqual(1, controller.remaining_iterations)
        self.assertEqual(1, malformed.snapshot.remaining_iterations)

        stale = controller.submit("snapshot-stale", "{not-even-decoded")
        self.assertIn("STALE_SNAPSHOT", _problem_codes(stale))
        self.assertEqual(0, controller.remaining_iterations)
        self.assertEqual(RepairState.STOPPED, controller.state)

        exhausted = controller.submit(stale.snapshot.snapshot_id, "{bad")
        self.assertIn("ITERATION_BUDGET_EXHAUSTED", _problem_codes(exhausted))
        self.assertEqual(0, controller.remaining_iterations)

    def test_safe_missing_duration_update_previews_and_commits(self) -> None:
        repository = _RecordingRepository(self._store())
        controller = self._controller(
            repository,
            budget=RepairBudget(
                max_iterations=3,
                max_provider_calls=2,
                max_changes=4,
                max_auto_changes_per_patch=4,
            ),
        )
        snapshot = controller.inspect()

        review = self._safe_duration_review(controller, snapshot)

        self.assertTrue(review.ready_to_commit, review.to_dict())
        self.assertEqual(RepairState.READY, review.state)
        self.assertEqual((), review.risk_codes)
        self.assertIsNone(review.required_approval_scope)
        self.assertIsNotNone(review.candidate_snapshot)
        self.assertNotIn(
            self._issue_id(snapshot, "MISSING_DURATION"),
            review.candidate_snapshot.issue_by_id,
        )
        # The evidence-backed duration write also invalidates prior evidence.
        self.assertEqual(2, review.change_count)
        self.assertEqual(2, len(review.changes))
        self.assertEqual(
            "set_activity_duration",
            review.reasons[0].option_key,
        )
        self.assertEqual(2, len(review.to_dict()["changes"]))

        result = controller.commit(review.review_id or "")

        self.assertTrue(result.applied, result.problems)
        self.assertEqual("applied", result.store_status)
        self.assertEqual(2, result.change_count)
        self.assertEqual(review.changes, result.changes)
        self.assertEqual(review.reasons, result.reasons)
        self.assertEqual(2, controller.remaining_changes)
        self.assertEqual(RepairState.WAITING_EXTERNAL, result.state)
        activity = self._store().load_plan()["state"]["itinerary"]["days"][0][
            "places"
        ][0]
        self.assertEqual(45, activity["duration_min"])
        self.assertEqual("unverified", activity["evidence_state"])
        self.assertEqual(2, len(repository.preview_calls))
        self.assertEqual(1, len(repository.apply_calls))
        for _, _, evaluation_at in (
            repository.preview_calls + repository.apply_calls
        ):
            self.assertEqual(_EVALUATION_AT_UTC, evaluation_at)

    def test_target_persistence_and_repeat_attempt_are_rejected(self) -> None:
        controller = self._controller()
        snapshot = controller.inspect()
        proposal = self._proposal(
            snapshot,
            UpdateActivity(
                "model-note",
                self.activity_id,
                {"note": "Does not repair duration"},
            ),
        )

        first = controller.submit(snapshot.snapshot_id, proposal)
        self.assertIn("TARGET_ISSUE_PERSISTS", _problem_codes(first))
        self.assertIsNone(first.review_id)

        second = controller.submit(first.snapshot.snapshot_id, proposal)
        self.assertIn("REPEATED_ATTEMPT", _problem_codes(second))
        self.assertIsNone(second.review_id)

    def test_preview_exception_is_sanitized_and_does_not_poison_attempt(self) -> None:
        repository = _ExplodingPreviewRepository(self._store())
        controller = self._controller(
            repository,
            budget=RepairBudget(
                max_iterations=2,
                max_provider_calls=0,
                max_changes=4,
                max_auto_changes_per_patch=4,
            ),
        )
        snapshot = controller.inspect()
        proposal = self._proposal(
            snapshot,
            UpdateActivity(
                "model-duration",
                self.activity_id,
                {"duration_min": 45},
            ),
        )

        first = controller.submit(snapshot.snapshot_id, proposal)
        second = controller.submit(first.snapshot.snapshot_id, proposal)

        self.assertEqual(
            {"REPOSITORY_PREVIEW_FAILED"},
            _problem_codes(first),
        )
        self.assertEqual(
            {"REPOSITORY_PREVIEW_FAILED"},
            _problem_codes(second),
        )
        self.assertNotIn("\n", first.problems[0].message)
        self.assertLessEqual(len(first.problems[0].message), 4096)
        self.assertEqual(2, len(repository.preview_calls))

    def test_candidate_that_only_moves_missing_duration_is_not_progress(self) -> None:
        controller = self._controller()
        snapshot = controller.inspect()
        proposal = self._proposal(
            snapshot,
            UpdateActivity(
                "model-fix-old",
                self.activity_id,
                {"duration_min": 45},
            ),
            AddActivity(
                op_id="model-add-new-gap",
                activity_id="activity-beta",
                day_id=self.day_id,
                fields={
                    "title": "Activity Beta",
                    "location_id": self.location_id,
                    "time": "11:00",
                    "decision_state": "selected",
                    "flexibility": "movable",
                },
            ),
        )

        review = controller.submit(snapshot.snapshot_id, proposal)

        self.assertIn("NO_STRICT_PROGRESS", _problem_codes(review))
        self.assertIsNone(review.review_id)
        self.assertIsNotNone(review.candidate_snapshot)

    def test_remove_activity_requires_exact_human_checkpoint(self) -> None:
        controller = self._controller()
        snapshot = controller.inspect()
        proposal = self._proposal(
            snapshot,
            RemoveActivity("model-remove", self.activity_id),
        )
        review = controller.submit(snapshot.snapshot_id, proposal)

        self.assertEqual(RepairState.WAITING_APPROVAL, review.state)
        self.assertIn("REMOVE_ACTIVITY", review.risk_codes)
        self.assertIn("OPTION_OPERATION_MISMATCH", review.risk_codes)

        denied = controller.commit(review.review_id or "")
        self.assertFalse(denied.applied)
        self.assertIn("HUMAN_CHECKPOINT_REQUIRED", _problem_codes(denied))

        wrong = controller.commit(
            review.review_id or "",
            human_grant=self._human_grant("review-wrong"),
        )
        self.assertFalse(wrong.applied)
        self.assertIn("HUMAN_CHECKPOINT_REQUIRED", _problem_codes(wrong))

        applied = controller.commit(
            review.review_id or "",
            human_grant=self._human_grant(review.review_id or ""),
        )
        self.assertTrue(applied.applied, applied.problems)

    def test_committed_decision_authority_requires_human_checkpoint(self) -> None:
        controller = self._controller()
        snapshot = controller.inspect()
        proposal = self._proposal(
            snapshot,
            UpdateActivity(
                "model-commit-decision",
                self.activity_id,
                {
                    "duration_min": 45,
                    "decision_state": "fixed",
                },
            ),
        )

        review = controller.submit(snapshot.snapshot_id, proposal)

        self.assertIn("CREATE_COMMITTED_DECISION", review.risk_codes)
        self.assertEqual(RepairState.WAITING_APPROVAL, review.state)
        denied = controller.commit(review.review_id or "")
        self.assertIn("HUMAN_CHECKPOINT_REQUIRED", _problem_codes(denied))
        applied = controller.commit(
            review.review_id or "",
            human_grant=self._human_grant(review.review_id or ""),
        )
        self.assertTrue(applied.applied, applied.problems)

    def test_option_operation_mismatch_requires_human_checkpoint(self) -> None:
        controller = self._controller()
        snapshot = controller.inspect()
        proposal = self._proposal(
            snapshot,
            UpdateActivity(
                "model-wide-update",
                self.activity_id,
                {
                    "duration_min": 45,
                    "note": "A wider effect than the selected option",
                },
            ),
        )

        review = controller.submit(snapshot.snapshot_id, proposal)

        self.assertEqual(
            ("OPTION_OPERATION_MISMATCH",),
            review.risk_codes,
        )
        denied = controller.commit(review.review_id or "")
        self.assertIn("HUMAN_CHECKPOINT_REQUIRED", _problem_codes(denied))
        applied = controller.commit(
            review.review_id or "",
            human_grant=self._human_grant(review.review_id or ""),
        )
        self.assertTrue(applied.applied, applied.problems)

    def test_decision_downgrade_is_detected_from_nested_canonical_state(self) -> None:
        self._write_plan(
            self._plan(
                decision_state="booked",
                flexibility="fixed_time",
            )
        )
        controller = self._controller()
        snapshot = controller.inspect()
        proposal = self._proposal(
            snapshot,
            UpdateActivity(
                "model-downgrade",
                self.activity_id,
                {
                    "duration_min": 45,
                    "decision_state": "selected",
                },
            ),
        )

        review = controller.submit(snapshot.snapshot_id, proposal)

        self.assertIn("DOWNGRADE_DECISION", review.risk_codes)
        self.assertEqual(RepairState.WAITING_APPROVAL, review.state)

    def test_store_approval_checkpoint_is_repreviewed_with_exact_grant(self) -> None:
        self._write_plan(
            self._plan(
                decision_state="fixed",
                flexibility="fixed_time",
            )
        )
        repository = _RecordingRepository(self._store())
        controller = self._controller(repository)
        snapshot = controller.inspect()
        review = self._safe_duration_review(controller, snapshot)

        self.assertEqual(RepairState.WAITING_APPROVAL, review.state)
        self.assertIsNone(review.candidate_snapshot)
        self.assertIsNotNone(review.required_approval_scope)
        self.assertEqual(1, len(repository.preview_calls))

        denied = controller.commit(review.review_id or "")
        self.assertIn("STORE_APPROVAL_REQUIRED", _problem_codes(denied))
        self.assertEqual(1, len(repository.preview_calls))

        grant = self._store_grant(review.required_approval_scope or "")
        applied = controller.commit(
            review.review_id or "",
            approvals=(grant,),
        )

        self.assertTrue(applied.applied, applied.problems)
        self.assertEqual(2, len(repository.preview_calls))
        self.assertEqual(1, len(repository.apply_calls))
        self.assertEqual((grant,), repository.preview_calls[1][1])
        self.assertEqual((grant,), repository.apply_calls[0][1])

    def test_change_budget_counts_derived_changes_and_fails_closed(self) -> None:
        controller = self._controller(
            budget=RepairBudget(
                max_iterations=2,
                max_provider_calls=0,
                max_changes=1,
                max_auto_changes_per_patch=1,
            )
        )
        snapshot = controller.inspect()

        review = self._safe_duration_review(controller, snapshot)

        self.assertIn("CHANGE_BUDGET_EXCEEDED", _problem_codes(review))
        self.assertEqual(RepairState.STOPPED, controller.state)
        self.assertEqual(1, controller.remaining_changes)
        self.assertNotIn(
            "duration_min",
            self._store().load_plan()["state"]["itinerary"]["days"][0][
                "places"
            ][0],
        )

    def test_provider_fingerprint_is_cached_only_after_completion(self) -> None:
        controller = self._controller(
            budget=RepairBudget(
                max_iterations=1,
                max_provider_calls=1,
                max_changes=1,
                max_auto_changes_per_patch=1,
            )
        )

        first = controller.reserve_provider_call("places:alpha")
        reserved = controller.reserve_provider_call("places:alpha")
        controller.complete_provider_call("places:alpha")
        cached = controller.reserve_provider_call("places:alpha")
        exhausted = controller.reserve_provider_call("routes:alpha")

        self.assertTrue(first.allowed)
        self.assertFalse(first.cached)
        self.assertEqual(0, first.remaining_provider_calls)
        self.assertFalse(reserved.allowed)
        self.assertFalse(reserved.cached)
        self.assertEqual(
            "PROVIDER_CALL_RESERVED",
            reserved.problem.code if reserved.problem else None,
        )
        self.assertTrue(cached.allowed)
        self.assertTrue(cached.cached)
        self.assertEqual(0, cached.remaining_provider_calls)
        self.assertFalse(exhausted.allowed)
        self.assertFalse(exhausted.cached)
        self.assertEqual(0, exhausted.remaining_provider_calls)
        self.assertEqual(
            "PROVIDER_BUDGET_EXHAUSTED",
            exhausted.problem.code if exhausted.problem else None,
        )

    def test_commit_outcome_unknown_retries_same_request_exactly_once(self) -> None:
        unknown = StoreResult(
            success=False,
            status="commit_outcome_unknown",
            action="patch",
            changed=False,
            problems=(
                StoreProblem(
                    "COMMIT_OUTCOME_UNKNOWN",
                    "fault after atomic replacement",
                ),
            ),
        )
        repository = _ScriptedApplyRepository(
            self._store(),
            (unknown, unknown),
        )
        controller = self._controller(repository)
        review = self._safe_duration_review(controller)

        result = controller.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertIn("COMMIT_NOT_CONFIRMED", _problem_codes(result))
        self.assertEqual(RepairState.STOPPED, result.state)
        self.assertEqual(2, len(repository.apply_calls))
        first_patch, first_approvals, first_at = repository.apply_calls[0]
        second_patch, second_approvals, second_at = repository.apply_calls[1]
        self.assertIs(first_patch, second_patch)
        self.assertEqual(first_approvals, second_approvals)
        self.assertEqual(first_at, second_at)
        self.assertEqual(_EVALUATION_AT_UTC, first_at)
        self.assertEqual(0, controller.budget.max_changes - controller.remaining_changes)

        controller.cancel_pending()
        controller.inspect()
        self.assertEqual(RepairState.STOPPED, controller.state)
        blocked = controller.commit(review.review_id or "")
        self.assertFalse(blocked.applied)
        self.assertIn("COMMIT_OUTCOME_UNKNOWN", _problem_codes(blocked))
        self.assertEqual(2, len(repository.apply_calls))

    def test_commit_outcome_unknown_can_resolve_as_exact_replay_or_apply(self) -> None:
        unknown = StoreResult(
            success=False,
            status="commit_outcome_unknown",
            action="patch",
            changed=False,
            problems=(
                StoreProblem(
                    "COMMIT_OUTCOME_UNKNOWN",
                    "uncertain durable outcome",
                ),
            ),
        )
        repository = _ScriptedApplyRepository(
            self._store(),
            (unknown, _DELEGATE),
        )
        controller = self._controller(repository)
        review = self._safe_duration_review(controller)

        result = controller.commit(review.review_id or "")

        self.assertTrue(result.applied, result.problems)
        self.assertEqual("applied", result.store_status)
        self.assertEqual(2, len(repository.apply_calls))

    def test_confirmed_apply_stays_applied_when_post_commit_read_fails(self) -> None:
        store = self._store()
        repository = _PostCommitReadFailureRepository(store)
        controller = self._controller(repository)
        review = self._safe_duration_review(controller)

        result = controller.commit(review.review_id or "")

        self.assertTrue(result.applied)
        self.assertEqual("applied", result.store_status)
        self.assertIn("POST_COMMIT_READ_FAILED", _problem_codes(result))
        self.assertEqual(2, result.change_count)
        self.assertEqual(2, len(result.changes))
        snapshot_activity = result.snapshot.state["itinerary"]["days"][0][
            "places"
        ][0]
        self.assertEqual(45, snapshot_activity["duration_min"])
        persisted = store.load_plan()["state"]["itinerary"]["days"][0][
            "places"
        ][0]
        self.assertEqual(45, persisted["duration_min"])

    def test_success_no_op_and_replayed_rolled_back_are_not_applied(self) -> None:
        outcomes = (
            StoreResult(
                success=True,
                status="no_op",
                action="patch",
                changed=False,
            ),
            StoreResult(
                success=True,
                status="replayed_rolled_back",
                action="patch",
                replayed=True,
                changed=False,
            ),
        )
        for outcome in outcomes:
            with self.subTest(status=outcome.status):
                repository = _ScriptedApplyRepository(
                    self._store(),
                    (outcome,),
                )
                controller = self._controller(repository)
                review = self._safe_duration_review(controller)

                result = controller.commit(review.review_id or "")

                self.assertFalse(result.applied)
                self.assertEqual(outcome.status, result.store_status)
                self.assertIn("COMMIT_NOT_CONFIRMED", _problem_codes(result))
                self.assertEqual(
                    controller.budget.max_changes,
                    controller.remaining_changes,
                )

    def test_cas_race_observation_participates_in_oscillation_detection(self) -> None:
        store = self._store()
        initial_plan = store.load_plan()
        initial_state_digest = self._controller(store).inspect().state_digest
        repository = _ExternalWinnerRepository(store)
        controller = self._controller(repository)
        review = self._safe_duration_review(controller)

        result = controller.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertIn("COMMIT_NOT_CONFIRMED", _problem_codes(result))
        self.assertIn("STALE_REVISION", _problem_codes(result))
        external_snapshot = controller.inspect()
        self.assertNotEqual(
            initial_state_digest,
            external_snapshot.state_digest,
        )

        current = store.load_plan()
        restored = build_plan(
            trip_id=current["trip_id"],
            generation=current["generation"] + 1,
            state=initial_plan["state"],
            receipts=current["receipts"],
        )
        self._write_plan(restored)

        repeated = controller.inspect()
        self.assertEqual(initial_state_digest, repeated.state_digest)
        self.assertEqual(RepairState.STOPPED, controller.state)

    def test_candidate_repeating_an_observed_semantic_state_is_rejected(self) -> None:
        store = self._store()
        controller = self._controller(store)
        initial = controller.inspect()
        initial_digest = initial.state_digest

        plan = store.load_plan()
        add = PlanPatch(
            trip_id=plan["trip_id"],
            base_revision=plan["revision"],
            idempotency_key="external-add-beta",
            operations=(
                AddActivity(
                    op_id="external-add",
                    activity_id="activity-beta",
                    day_id=self.day_id,
                    fields={
                        "title": "Activity Beta",
                        "location_id": self.location_id,
                        "time": "11:00",
                        "decision_state": "selected",
                        "flexibility": "movable",
                    },
                ),
            ),
            intent="Create a worse state observed by the controller.",
        )
        external = store.apply_patch(add, evaluation_at=_EVALUATION_AT_UTC)
        self.assertTrue(external.success, external.to_dict())
        changed = controller.inspect()
        self.assertNotEqual(initial_digest, changed.state_digest)

        proposal = self._proposal(
            changed,
            RemoveActivity("model-remove-beta", "activity-beta"),
            issue_activity_id="activity-beta",
        )
        review = controller.submit(changed.snapshot_id, proposal)

        self.assertIn("OSCILLATION_DETECTED", _problem_codes(review))
        self.assertIsNotNone(review.candidate_snapshot)
        self.assertEqual(
            initial_digest,
            review.candidate_snapshot.state_digest,
        )
        self.assertEqual(RepairState.STOPPED, controller.state)

        inspected = controller.inspect()
        self.assertEqual(RepairState.STOPPED, controller.state)
        blocked = controller.submit(inspected.snapshot_id, proposal)
        self.assertEqual(
            {"OSCILLATION_DETECTED"},
            _problem_codes(blocked),
        )
        provider = controller.reserve_provider_call("places:blocked")
        self.assertFalse(provider.allowed)
        self.assertEqual(
            "OSCILLATION_DETECTED",
            provider.problem.code if provider.problem else None,
        )

    def test_repreview_must_reproduce_exact_candidate_state(self) -> None:
        repository = _NondeterministicPreviewRepository(self._store())
        controller = self._controller(repository)
        review = self._safe_duration_review(controller)

        result = controller.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertIn("NONDETERMINISTIC_PREVIEW", _problem_codes(result))
        self.assertEqual(RepairState.STOPPED, result.state)
        self.assertEqual(2, len(repository.preview_calls))
        self.assertEqual(0, len(repository.apply_calls))


if __name__ == "__main__":
    unittest.main()
