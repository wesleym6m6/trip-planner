"""Adversarial offline tests for schedule review and commit staging."""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from dataclasses import fields, replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from trip_planner.codec import build_plan, compute_revision, encode_plan
from trip_planner.composition import compose_trip_state
from trip_planner.evidence_store import EvidenceStore
from trip_planner.facts import (
    EvidenceLedger,
    EvidencePersistence,
    EvidenceSnapshot,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
    ProviderProvenance,
    ProviderPolicy,
    ProviderPolicyRegistry,
    ProviderRequest,
    ProviderResult,
    ProviderResultStatus,
    authorize_provider_result,
    google_maps_policy_registry,
    merge_provider_result,
)
from trip_planner.models import CheckReport, CheckStatus
from trip_planner.mutations import (
    ApprovalGrant,
    ChangeRecord,
    PlanPatch,
    UpdateActivity,
)
from trip_planner.repair_loop import HumanCheckpointGrant
from trip_planner.schedule_staging import (
    ScheduleCommitResult,
    ScheduleStageProblem,
    ScheduleStageReview,
    ScheduleStageState,
    ScheduleStager,
)
from trip_planner.scheduler import SOLVER_VERSION
from trip_planner.scheduling import (
    ScheduleAssignment,
    ScheduleContractError,
    ScheduleProblem,
    ScheduleScore,
    build_schedule_candidate,
    schedule_problem_from_composed,
    schedule_problem_from_plan,
)
from trip_planner.store import StoreResult, TripStore


EVALUATION_AT = datetime(2026, 7, 28, tzinfo=timezone.utc)
_DELEGATE = object()


def _problem_codes(value: Any) -> set[str]:
    return {problem.code for problem in value.problems}


class _EvidenceSource:
    def __init__(self, *store_revisions: str) -> None:
        if not store_revisions:
            raise ValueError("at least one store revision is required")
        self.store_revisions = list(store_revisions)
        self.calls = 0
        self.policies = google_maps_policy_registry(
            GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
        )

    def load(self) -> "_EvidenceSource":
        return self

    def snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        index = min(self.calls, len(self.store_revisions) - 1)
        self.calls += 1
        return EvidenceSnapshot.from_ledger(
            EvidenceLedger(policies=self.policies),
            evaluation_at=evaluation_at,
            purge_now=evaluation_at,
            store_revision=self.store_revisions[index],
        )


class _SnapshotEvidenceSource:
    def __init__(self, *snapshots: EvidenceSnapshot) -> None:
        if not snapshots:
            raise ValueError("at least one snapshot is required")
        self.snapshots = snapshots
        self.calls = 0

    def load(self) -> "_SnapshotEvidenceSource":
        return self

    def snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        index = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        snapshot = self.snapshots[index]
        if snapshot.evaluation_at != evaluation_at:
            raise AssertionError("unexpected evidence evaluation clock")
        return snapshot


def _route_snapshot(
    duration_min: float,
    *,
    store_revision: str,
) -> EvidenceSnapshot:
    policies = ProviderPolicyRegistry(
        policies=(
            ProviderPolicy(
                policy_id="schedule-route-runtime-v1",
                provider_id="schedule-route",
                adapter_id="schedule-route",
                adapter_version="v1",
                contract_region="test",
                allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
                allowed_value_fields=("duration_min", "mode"),
                allowed_operations=("compute-route",),
                persistence=EvidencePersistence.MEMORY_ONLY,
                max_validity_seconds=2 * 60 * 60,
                max_retention_seconds=3 * 60 * 60,
            ),
        )
    )
    policy = policies.policy("schedule-route-runtime-v1")
    key = FactKey(
        kind=FactKind.ROUTE_ESTIMATE,
        subject_ids=("location-alpha", "location-beta"),
        qualifiers=(("mode", "walking"),),
    )
    request = ProviderRequest(
        provider_id="schedule-route",
        adapter_id="schedule-route",
        adapter_version="v1",
        operation="compute-route",
        fact_keys=(key,),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
    )
    retrieved_at = EVALUATION_AT - timedelta(hours=1)
    observation = FactObservation(
        key=key,
        value=FactValue.from_payload(
            FactKind.ROUTE_ESTIMATE,
            {
                "mode": "walking",
                "duration_min": duration_min,
            },
        ),
        provenance=ProviderProvenance(
            provider_id="schedule-route",
            adapter_id="schedule-route",
            adapter_version="v1",
            request_fingerprint=request.request_fingerprint,
            retention_policy_id=policy.policy_id,
            response_id="runtime-route-response",
            source_uri="https://example.test/runtime-route-source",
            attributions=(),
        ),
        retrieved_at=retrieved_at,
        valid_until=EVALUATION_AT + timedelta(hours=1),
        purge_at=EVALUATION_AT + timedelta(hours=2),
        confidence=1.0,
    )
    result = ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=ProviderResultStatus.SUCCESS,
        observations=(observation,),
        problems=(),
        attempts_used=1,
        completed_at=retrieved_at + timedelta(minutes=1),
    )
    authorized = authorize_provider_result(request, result, policies)
    merged = merge_provider_result(
        EvidenceLedger(policies),
        authorized,
        purge_now=EVALUATION_AT,
    )
    return EvidenceSnapshot.from_ledger(
        merged.ledger,
        evaluation_at=EVALUATION_AT,
        purge_now=EVALUATION_AT,
        store_revision=store_revision,
    )


def _hours_snapshot(
    *,
    closed: bool,
    store_revision: str,
) -> EvidenceSnapshot:
    policies = ProviderPolicyRegistry(
        policies=(
            ProviderPolicy(
                policy_id="schedule-hours-runtime-v1",
                provider_id="schedule-hours",
                adapter_id="schedule-hours",
                adapter_version="v1",
                contract_region="test",
                allowed_fact_kinds=(FactKind.PLACE_OPENING_HOURS,),
                allowed_value_fields=(
                    "basis",
                    "closed_dates",
                    "coverage_end",
                    "coverage_start",
                    "intervals",
                    "provider_place_id",
                    "timezone",
                ),
                allowed_operations=("fetch-opening-hours",),
                persistence=EvidencePersistence.MEMORY_ONLY,
                max_validity_seconds=2 * 60 * 60,
                max_retention_seconds=3 * 60 * 60,
            ),
        )
    )
    policy = policies.policy("schedule-hours-runtime-v1")
    key = FactKey(
        kind=FactKind.PLACE_OPENING_HOURS,
        subject_ids=("location-alpha",),
        qualifiers=(
            ("basis", "current"),
            ("identity_provider", "schedule-hours"),
            ("provider_place_id", "place-alpha"),
            ("target_end", "2026-07-28"),
            ("target_start", "2026-07-28"),
        ),
    )
    request = ProviderRequest(
        provider_id="schedule-hours",
        adapter_id="schedule-hours",
        adapter_version="v1",
        operation="fetch-opening-hours",
        fact_keys=(key,),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
    )
    retrieved_at = EVALUATION_AT - timedelta(minutes=1)
    observation = FactObservation(
        key=key,
        value=FactValue.from_payload(
            FactKind.PLACE_OPENING_HOURS,
            {
                "provider_place_id": "place-alpha",
                "timezone": "Asia/Seoul",
                "basis": "current",
                "coverage_start": "2026-07-28",
                "coverage_end": "2026-07-28",
                "intervals": (
                    []
                    if closed
                    else [
                        {
                            "start_at": "2026-07-28T08:00:00+09:00",
                            "end_at": "2026-07-28T18:00:00+09:00",
                        }
                    ]
                ),
                "closed_dates": ["2026-07-28"] if closed else [],
            },
        ),
        provenance=ProviderProvenance(
            provider_id="schedule-hours",
            adapter_id="schedule-hours",
            adapter_version="v1",
            request_fingerprint=request.request_fingerprint,
            retention_policy_id=policy.policy_id,
            response_id=("hours-closed" if closed else "hours-open"),
            source_uri="https://example.test/hours-source",
            attributions=(),
        ),
        retrieved_at=retrieved_at,
        valid_until=EVALUATION_AT + timedelta(hours=1),
        purge_at=EVALUATION_AT + timedelta(hours=2),
        confidence=1.0,
    )
    result = ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=ProviderResultStatus.SUCCESS,
        observations=(observation,),
        problems=(),
        attempts_used=1,
        completed_at=EVALUATION_AT,
    )
    authorized = authorize_provider_result(request, result, policies)
    merged = merge_provider_result(
        EvidenceLedger(policies),
        authorized,
        purge_now=EVALUATION_AT,
    )
    return EvidenceSnapshot.from_ledger(
        merged.ledger,
        evaluation_at=EVALUATION_AT,
        purge_now=EVALUATION_AT,
        store_revision=store_revision,
    )


class _RecordingRepository:
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
            raise AssertionError("unexpected extra apply call")
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
        if len(self.preview_calls) != 2 or result.draft is None:
            return result
        injected = ChangeRecord(
            op_id="injected-preview-drift",
            entity_type="day",
            entity_id="day-1",
            field="title",
            before="Fixture day",
            after="Drifted",
        )
        return replace(
            result,
            draft=replace(
                result.draft,
                changes=(*result.draft.changes, injected),
            ),
        )


class _ExternalWinnerRepository(_RecordingRepository):
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
            idempotency_key="external-schedule-winner",
            operations=(
                UpdateActivity(
                    "external-alpha",
                    "activity-alpha",
                    {
                        "note": "external writer won",
                        "time": "09:00:00",
                    },
                ),
                UpdateActivity(
                    "external-beta",
                    "activity-beta",
                    {"time": "10:00:00"},
                ),
            ),
            intent="simulate schedule CAS race",
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


class _UnrelatedWinnerOnPreviewRepository(_RecordingRepository):
    def preview_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        if len(self.preview_calls) + 1 == 2:
            current = self.store.load_plan()
            external = PlanPatch(
                trip_id=current["trip_id"],
                base_revision=current["revision"],
                idempotency_key="external-preview-winner",
                operations=(
                    UpdateActivity(
                        "external-preview-note",
                        "activity-alpha",
                        {
                            "note": "preview race winner",
                            "time": "09:00:00",
                        },
                    ),
                    UpdateActivity(
                        "external-preview-beta",
                        "activity-beta",
                        {"time": "10:00:00"},
                    ),
                ),
                intent="simulate unrelated preview winner",
            )
            won = self.store.apply_patch(
                external,
                evaluation_at=evaluation_at,
            )
            if not won.success:
                raise AssertionError(won.to_dict())
        return super().preview_patch(
            patch,
            approvals,
            evaluation_at=evaluation_at,
        )


class _FailNthLoadRepository(_RecordingRepository):
    def __init__(self, store: TripStore, *, fail_on: int) -> None:
        super().__init__(store)
        self.fail_on = fail_on
        self.load_calls = 0

    def load_plan(self) -> dict[str, Any]:
        self.load_calls += 1
        if self.load_calls == self.fail_on:
            raise RuntimeError("transient canonical read failure")
        return super().load_plan()


class _TamperedCommitResultRepository(_RecordingRepository):
    def __init__(self, store: TripStore, *, mode: str) -> None:
        super().__init__(store)
        self.mode = mode

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
        if self.mode == "missing":
            return replace(
                result,
                candidate_plan=None,
                check_report=None,
            )
        if self.mode == "diverged_infeasible":
            candidate = result.mutable_candidate_plan()
            if candidate is None:
                raise AssertionError("expected applied candidate plan")
            candidate = deepcopy(candidate)
            candidate["state"]["itinerary"]["days"][0]["places"][0][
                "time"
            ] = "11:00:00"
            candidate["revision"] = compute_revision(candidate)
            return replace(result, candidate_plan=candidate)
        raise AssertionError(f"unknown tamper mode {self.mode!r}")


class _ExactWinnerOnPreviewRepository(_RecordingRepository):
    def __init__(self, store: TripStore, *, preview_call: int) -> None:
        super().__init__(store)
        self.preview_call = preview_call

    def preview_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        if len(self.preview_calls) + 1 == self.preview_call:
            won = self.store.apply_patch(
                patch,
                approvals,
                evaluation_at=evaluation_at,
            )
            if not won.success:
                raise AssertionError(won.to_dict())
        return super().preview_patch(
            patch,
            approvals,
            evaluation_at=evaluation_at,
        )


class _ApplyRollbackOnPreviewRepository(_RecordingRepository):
    def __init__(self, store: TripStore, *, preview_call: int) -> None:
        super().__init__(store)
        self.preview_call = preview_call

    def preview_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        if len(self.preview_calls) + 1 == self.preview_call:
            applied = self.store.apply_patch(
                patch,
                approvals,
                evaluation_at=evaluation_at,
            )
            if not applied.success or applied.transaction_id is None:
                raise AssertionError(applied.to_dict())
            rolled_back = self.store.rollback(
                applied.transaction_id,
                applied.current_revision or "",
                "rollback-exact-schedule-winner",
                evaluation_at=evaluation_at,
            )
            if not rolled_back.success:
                raise AssertionError(rolled_back.to_dict())
        return super().preview_patch(
            patch,
            approvals,
            evaluation_at=evaluation_at,
        )


class _RaiseAfterDurableApplyRepository(_RecordingRepository):
    def __init__(self, store: TripStore) -> None:
        super().__init__(store)
        self.raised = False

    def apply_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        dry_run: bool = False,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        self.apply_calls.append((patch, tuple(approvals), evaluation_at))
        result = self.store.apply_patch(
            patch,
            approvals,
            dry_run=dry_run,
            evaluation_at=evaluation_at,
        )
        if not self.raised:
            self.raised = True
            raise RuntimeError("adapter raised after durable apply")
        return result


class _FakeAppliedRepository(_RecordingRepository):
    def apply_patch(
        self,
        patch: Any,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        dry_run: bool = False,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        self.apply_calls.append((patch, tuple(approvals), evaluation_at))
        preview = self.store.preview_patch(
            patch,
            approvals,
            evaluation_at=evaluation_at,
        )
        return replace(
            preview,
            success=True,
            status="applied",
            transaction_id="fake-ack-transaction",
            current_revision=preview.applied_revision,
            replayed=False,
            dry_run=False,
            changed=True,
        )


class _ExtraWriteAfterApplyRepository(_RecordingRepository):
    def __init__(self, store: TripStore, *, mode: str) -> None:
        super().__init__(store)
        self.mode = mode

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
        if not result.success:
            return result
        plan = self.store.load_plan()
        if self.mode == "state":
            plan["state"]["trip"]["title"] = "Unreviewed extra write"
        elif self.mode != "generation":
            raise AssertionError(f"unknown extra-write mode {self.mode!r}")
        plan["generation"] += 1
        plan["revision"] = compute_revision(plan)
        receipt = plan["receipts"][patch.idempotency_key]
        receipt["applied_revision"] = plan["revision"]
        receipt["applied_generation"] = plan["generation"]
        self.store.plan_path.write_bytes(encode_plan(plan))
        return result


class _TamperedPreviewRepository(_RecordingRepository):
    def __init__(self, store: TripStore, *, mode: str) -> None:
        super().__init__(store)
        self.mode = mode

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
        if self.mode == "current_revision":
            return replace(result, current_revision="wrong-current-revision")
        if self.mode == "draft_digest":
            if result.draft is None:
                raise AssertionError("expected preview draft")
            return replace(
                result,
                draft=replace(
                    result.draft,
                    patch_digest="sha256:" + ("0" * 64),
                ),
            )
        candidate = result.mutable_candidate_plan()
        if candidate is None:
            raise AssertionError("expected candidate plan")
        if self.mode == "trip_id":
            candidate["trip_id"] = "wrong-canonical-trip"
        elif self.mode == "state_title":
            candidate["state"]["trip"]["title"] = "Unreviewed title change"
        else:
            raise AssertionError(f"unknown preview tamper mode {self.mode!r}")
        candidate["revision"] = compute_revision(candidate)
        return replace(result, candidate_plan=candidate)


class _EvilScore(ScheduleScore):
    def __eq__(self, other: object) -> bool:
        return True

    def objective_key(self) -> tuple[int, ...]:
        return (-999,) + (0,) * 16


class Phase3ScheduleStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.trips_root = Path(self.temporary.name) / "trips"
        self.slug = "schedule-stage-fixture"
        self.data_dir = self.trips_root / self.slug / "data"
        self.data_dir.mkdir(parents=True)
        self.plan_path = self.data_dir / "plan.json"
        self._write_plan(self._plan())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _activity(
        self,
        activity_id: str,
        *,
        start: str,
        location_id: str,
        window: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "activity_id": activity_id,
            "title": activity_id.replace("-", " ").title(),
            "location_id": location_id,
            "time": start,
            "duration_min": 30,
            "decision_state": "selected",
            "flexibility": "movable",
            "evidence_state": "verified",
            "note": "",
        }
        if window is not None:
            result["allowed_windows"] = [
                {"start": window[0], "end": window[1]}
            ]
        return result

    def _plan(
        self,
        *,
        protected: bool = False,
        alpha_start: str = "11:00",
    ) -> dict[str, Any]:
        trip: dict[str, Any] = {
            "slug": self.slug,
            "title": "Schedule stage fixture",
            "timezone": "Asia/Seoul",
            "date_range": "2026-10-01 ~ 2026-10-01",
            "cities": ["Fixture City"],
            "constraints": [],
        }
        if protected:
            trip["_trip_planner"] = {
                "migration": {
                    "source_schema": "legacy-v1",
                    "source_revision": "fixture-source",
                    "protected_activity_ids": [
                        "activity-alpha",
                        "activity-beta",
                    ],
                    "ignored_travel_edges": [],
                }
            }
        return build_plan(
            trip_id="canonical-schedule-stage",
            generation=1,
            state={
                "trip": trip,
                "itinerary": {
                    "available_modes": ["walking"],
                    "days": [
                        {
                            "day_id": "day-1",
                            "day": 1,
                            "date": "2026-10-01",
                            "title": "Fixture day",
                            "timezone": "Asia/Seoul",
                            "available_start": "08:00",
                            "available_end": "18:00",
                            "start_location_id": "location-alpha",
                            "end_location_id": "location-beta",
                            "allowed_modes": ["walking"],
                            "places": [
                                self._activity(
                                    "activity-alpha",
                                    start=alpha_start,
                                    location_id="location-alpha",
                                    window=("09:00", "10:00"),
                                ),
                                self._activity(
                                    "activity-beta",
                                    start="12:00",
                                    location_id="location-beta",
                                ),
                            ],
                            "travel": [
                                {
                                    "from_activity_id": "activity-alpha",
                                    "to_activity_id": "activity-beta",
                                    "recommended_mode": "walking",
                                    "modes": {
                                        "walking": {
                                            "duration_min": 10,
                                            "evidence_state": "verified",
                                        }
                                    },
                                }
                            ],
                        }
                    ],
                },
            },
        )

    def _write_plan(self, plan: dict[str, Any]) -> None:
        self.plan_path.write_bytes(encode_plan(plan))

    def _store(self) -> TripStore:
        return TripStore(self.trips_root, self.slug)

    def _problem_candidate(
        self,
        plan: dict[str, Any] | None = None,
        *,
        alpha_time: time = time(9),
        beta_time: time = time(10),
    ) -> tuple[ScheduleProblem, Any]:
        problem = schedule_problem_from_plan(
            plan or self._store().load_plan(),
            evaluation_at=EVALUATION_AT,
        )
        candidate = build_schedule_candidate(
            problem,
            (
                ScheduleAssignment(
                    "activity-alpha",
                    "day-1",
                    0,
                    alpha_time,
                ),
                ScheduleAssignment(
                    "activity-beta",
                    "day-1",
                    1,
                    beta_time,
                ),
            ),
            solver=SOLVER_VERSION,
        )
        return problem, candidate

    def _evidence_problem_candidate(
        self,
        source: _EvidenceSource,
    ) -> tuple[ScheduleProblem, Any]:
        plan = self._store().load_plan()
        composed = compose_trip_state(
            plan,
            source.snapshot(evaluation_at=EVALUATION_AT),
        )
        problem = schedule_problem_from_composed(composed)
        candidate = build_schedule_candidate(
            problem,
            (
                ScheduleAssignment(
                    "activity-alpha",
                    "day-1",
                    0,
                    time(9),
                ),
                ScheduleAssignment(
                    "activity-beta",
                    "day-1",
                    1,
                    time(10),
                ),
            ),
            solver=SOLVER_VERSION,
        )
        return problem, candidate

    def _external_note(self, key: str) -> StoreResult:
        current = self._store().load_plan()
        return self._store().apply_patch(
            PlanPatch(
                trip_id=current["trip_id"],
                base_revision=current["revision"],
                idempotency_key=key,
                operations=(
                    UpdateActivity(
                        f"op-{key}",
                        "activity-alpha",
                        {"note": key},
                    ),
                ),
            ),
            evaluation_at=EVALUATION_AT,
        )

    def test_stage_and_commit_recompute_persisted_waiting_external(self) -> None:
        repository = _RecordingRepository(self._store())
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-core",
            max_auto_changes=8,
        )

        review = stager.stage_schedule_candidate(problem, candidate)

        self.assertTrue(review.ready_to_commit, review.to_dict())
        self.assertEqual(ScheduleStageState.READY, review.state)
        self.assertEqual("hard_violation_count", review.decisive_objective)
        self.assertEqual(("day-1",), review.invalidated_day_ids)
        self.assertEqual("needs_verification", review.post_preview_status)
        self.assertIsNone(review.required_approval_scope)

        result = stager.commit(review.review_id or "")

        self.assertTrue(result.applied, result.to_dict())
        self.assertEqual(ScheduleStageState.WAITING_EXTERNAL, result.state)
        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, result.check_report.status)
        self.assertTrue(result.candidate_is_current)
        self.assertFalse(result.replayed)
        self.assertEqual(("day-1",), result.invalidated_day_ids)
        self.assertEqual(candidate.required_arc_keys, result.required_arc_keys)

    def test_evidence_drift_rejects_stage_before_preview(self) -> None:
        source = _EvidenceSource("1" * 64, "2" * 64)
        problem, candidate = self._evidence_problem_candidate(source)
        repository = _RecordingRepository(self._store())
        stager = ScheduleStager(
            repository,
            run_id="schedule-evidence-stage-drift",
            evidence_source=source,
        )

        review = stager.stage_schedule_candidate(problem, candidate)

        self.assertEqual(ScheduleStageState.REJECTED, review.state)
        self.assertIn("EVIDENCE_REVISION_CHANGED", _problem_codes(review))
        self.assertEqual([], repository.preview_calls)

    def test_evidence_bound_problem_requires_runtime_source(self) -> None:
        source = _EvidenceSource("1" * 64)
        problem, candidate = self._evidence_problem_candidate(source)
        repository = _RecordingRepository(self._store())
        stager = ScheduleStager(
            repository,
            run_id="schedule-evidence-source-required",
        )

        review = stager.stage_schedule_candidate(problem, candidate)

        self.assertEqual(ScheduleStageState.REJECTED, review.state)
        self.assertIn("EVIDENCE_SOURCE_REQUIRED", _problem_codes(review))
        self.assertEqual([], repository.preview_calls)

    def test_evidence_store_source_uses_load_result_snapshot(self) -> None:
        """The source itself need only implement EvidenceSource.load()."""

        policies = google_maps_policy_registry(
            GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
        )
        source = EvidenceStore(
            self.trips_root,
            self.slug,
            "canonical-schedule-stage",
            policies,
            clock=lambda: EVALUATION_AT,
        )
        self.assertFalse(callable(getattr(source, "snapshot", None)))
        plan = self._store().load_plan()
        composed = compose_trip_state(
            plan,
            source.load().snapshot(evaluation_at=EVALUATION_AT),
        )
        problem = schedule_problem_from_composed(composed)
        candidate = build_schedule_candidate(
            problem,
            (
                ScheduleAssignment("activity-alpha", "day-1", 0, time(9)),
                ScheduleAssignment("activity-beta", "day-1", 1, time(10)),
            ),
            solver=SOLVER_VERSION,
        )

        stager = ScheduleStager(
            _RecordingRepository(self._store()),
            run_id="schedule-evidence-store-source",
            max_auto_changes=8,
            evidence_source=source,
        )
        review = stager.stage_schedule_candidate(problem, candidate)

        self.assertTrue(review.ready_to_commit, review.to_dict())

    def test_evidence_drift_clears_review_before_commit_write(self) -> None:
        source = _EvidenceSource("1" * 64, "1" * 64, "2" * 64)
        problem, candidate = self._evidence_problem_candidate(source)
        repository = _RecordingRepository(self._store())
        stager = ScheduleStager(
            repository,
            run_id="schedule-evidence-commit-drift",
            max_auto_changes=8,
            evidence_source=source,
        )
        review = stager.stage_schedule_candidate(problem, candidate)
        self.assertTrue(review.ready_to_commit, review.to_dict())
        self.assertEqual(2, source.calls)

        result = stager.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertIn("EVIDENCE_REVISION_CHANGED", _problem_codes(result))
        self.assertEqual([], repository.apply_calls)
        self.assertFalse(stager.has_pending_review)

    def test_postcommit_evidence_drift_is_applied_but_waiting_external(
        self,
    ) -> None:
        source = _EvidenceSource(
            "1" * 64,
            "1" * 64,
            "1" * 64,
            "2" * 64,
        )
        problem, candidate = self._evidence_problem_candidate(source)
        repository = _RecordingRepository(self._store())
        stager = ScheduleStager(
            repository,
            run_id="schedule-evidence-postcommit-drift",
            max_auto_changes=8,
            evidence_source=source,
        )
        review = stager.stage_schedule_candidate(problem, candidate)
        self.assertTrue(review.ready_to_commit, review.to_dict())

        result = stager.commit(review.review_id or "")

        self.assertTrue(result.applied, result.to_dict())
        self.assertEqual(ScheduleStageState.WAITING_EXTERNAL, result.state)
        self.assertIn("EVIDENCE_REVISION_CHANGED", _problem_codes(result))
        self.assertFalse(result.candidate_is_current)
        self.assertEqual(4, source.calls)
        current = self._store().load_plan()
        day = current["state"]["itinerary"]["days"][0]
        self.assertEqual([], day["travel"])
        self.assertEqual(
            ["09:00:00", "10:00:00"],
            [activity["time"] for activity in day["places"]],
        )
        self.assertEqual(2, len(repository.preview_calls))
        self.assertEqual(1, len(repository.apply_calls))

    def test_postcommit_infeasible_evidence_drift_is_known_applied(
        self,
    ) -> None:
        short_route = _route_snapshot(
            10,
            store_revision="1" * 64,
        )
        long_route = _route_snapshot(
            120,
            store_revision="2" * 64,
        )
        source = _SnapshotEvidenceSource(
            short_route,
            short_route,
            short_route,
            long_route,
        )
        problem, candidate = self._evidence_problem_candidate(source)
        repository = _RecordingRepository(self._store())
        stager = ScheduleStager(
            repository,
            run_id="schedule-evidence-postcommit-infeasible",
            max_auto_changes=8,
            evidence_source=source,
        )
        review = stager.stage_schedule_candidate(problem, candidate)
        self.assertTrue(review.ready_to_commit, review.to_dict())

        result = stager.commit(review.review_id or "")

        self.assertTrue(result.applied, result.to_dict())
        self.assertEqual(ScheduleStageState.WAITING_EXTERNAL, result.state)
        self.assertEqual(CheckStatus.INFEASIBLE, result.check_report.status)
        self.assertIn("EVIDENCE_REVISION_CHANGED", _problem_codes(result))
        self.assertIn("POST_COMMIT_PLAN_INFEASIBLE", _problem_codes(result))
        self.assertFalse(result.candidate_is_current)
        self.assertEqual(4, source.calls)
        current = self._store().load_plan()
        day = current["state"]["itinerary"]["days"][0]
        self.assertEqual(
            ["09:00:00", "10:00:00"],
            [activity["time"] for activity in day["places"]],
        )

    def test_postcommit_hours_drift_reports_current_closure(self) -> None:
        plan = self._plan()
        plan["state"]["trip"]["date_range"] = "2026-07-28 ~ 2026-07-28"
        plan["state"]["itinerary"]["days"][0]["date"] = "2026-07-28"
        plan["revision"] = compute_revision(plan)
        self._write_plan(plan)
        open_hours = _hours_snapshot(closed=False, store_revision="1" * 64)
        closed_hours = _hours_snapshot(closed=True, store_revision="2" * 64)
        source = _SnapshotEvidenceSource(
            open_hours,
            open_hours,
            open_hours,
            closed_hours,
        )
        composed = compose_trip_state(
            self._store().load_plan(),
            source.snapshot(evaluation_at=EVALUATION_AT),
        )
        problem = schedule_problem_from_composed(composed)
        candidate = build_schedule_candidate(
            problem,
            (
                ScheduleAssignment("activity-alpha", "day-1", 0, time(9)),
                ScheduleAssignment("activity-beta", "day-1", 1, time(10)),
            ),
            solver=SOLVER_VERSION,
        )
        stager = ScheduleStager(
            _RecordingRepository(self._store()),
            run_id="schedule-hours-postcommit-drift",
            max_auto_changes=8,
            evidence_source=source,
        )

        review = stager.stage_schedule_candidate(problem, candidate)
        self.assertTrue(review.ready_to_commit, review.to_dict())
        result = stager.commit(review.review_id or "")

        self.assertTrue(result.applied, result.to_dict())
        self.assertEqual(ScheduleStageState.WAITING_EXTERNAL, result.state)
        self.assertIn("EVIDENCE_REVISION_CHANGED", _problem_codes(result))
        self.assertFalse(result.candidate_is_current)
        self.assertEqual(CheckStatus.INFEASIBLE, result.check_report.status)
        self.assertIn(
            "OPENING_HOURS_VIOLATION",
            {item.code for item in result.check_report.issues},
        )

    def test_evidence_bound_public_values_redact_runtime_scores(
        self,
    ) -> None:
        source = _EvidenceSource(
            "1" * 64,
            "1" * 64,
            "1" * 64,
            "1" * 64,
        )
        problem, candidate = self._evidence_problem_candidate(source)
        marker = 12345
        marked_candidate = replace(
            candidate,
            score=replace(candidate.score, travel_deci_min=marker),
            report=CheckReport(
                status=CheckStatus.FEASIBLE,
                metrics=(("provider_route_marker", float(marker)),),
            ),
        )
        self.assertNotIn(str(marker), repr(marked_candidate))

        stager = ScheduleStager(
            _RecordingRepository(self._store()),
            run_id="schedule-evidence-redaction",
            max_auto_changes=8,
            evidence_source=source,
        )
        review = stager.stage_schedule_candidate(problem, candidate)
        self.assertTrue(review.ready_to_commit, review.to_dict())
        marked_review = replace(
            review,
            baseline_score=replace(
                review.baseline_score,
                travel_deci_min=marker,
            ),
            candidate_score=replace(
                review.candidate_score,
                travel_deci_min=marker,
            ),
        )
        self.assertNotIn(str(marker), repr(marked_review))
        self.assertNotIn(str(marker), repr(marked_review.to_dict()))
        self.assertEqual(
            {"redacted", "digest"},
            set(marked_review.to_dict()["candidate_score"]),
        )

        result = stager.commit(review.review_id or "")
        self.assertTrue(result.applied, result.to_dict())
        marked_result = replace(
            result,
            check_report=CheckReport(
                status=result.check_report.status,
                metrics=(("provider_route_marker", float(marker)),),
            ),
        )
        self.assertNotIn(str(marker), repr(marked_result))
        self.assertNotIn(str(marker), repr(marked_result.to_dict()))

    def test_commit_ack_payload_is_ignored_after_canonical_reload(
        self,
    ) -> None:
        for mode in ("missing", "diverged_infeasible"):
            with self.subTest(mode=mode):
                self._write_plan(self._plan())
                repository = _TamperedCommitResultRepository(
                    self._store(),
                    mode=mode,
                )
                problem, candidate = self._problem_candidate()
                stager = ScheduleStager(
                    repository,
                    run_id=f"schedule-run-post-commit-{mode}",
                )
                review = stager.stage_schedule_candidate(
                    problem, candidate
                )

                result = stager.commit(review.review_id or "")

                self.assertTrue(result.applied, result.to_dict())
                self.assertEqual(
                    ScheduleStageState.WAITING_EXTERNAL,
                    result.state,
                )
                self.assertTrue(result.candidate_is_current)
                self.assertEqual(set(), _problem_codes(result))

    def test_post_commit_full_state_and_generation_are_exactly_bound(
        self,
    ) -> None:
        for mode, expected_code in (
            ("state", "COMMIT_STATE_DIVERGED"),
            ("generation", "COMMIT_REVISION_DIVERGED"),
        ):
            with self.subTest(mode=mode):
                self._write_plan(self._plan())
                repository = _ExtraWriteAfterApplyRepository(
                    self._store(),
                    mode=mode,
                )
                problem, candidate = self._problem_candidate()
                stager = ScheduleStager(
                    repository,
                    run_id=f"schedule-run-extra-write-{mode}",
                )
                review = stager.stage_schedule_candidate(problem, candidate)

                result = stager.commit(review.review_id or "")

                self.assertTrue(result.applied, result.to_dict())
                self.assertFalse(result.candidate_is_current)
                self.assertEqual(
                    ScheduleStageState.OUTCOME_UNKNOWN,
                    result.state,
                )
                self.assertIn(expected_code, _problem_codes(result))

    def test_feasible_baseline_rejects_worse_time_churn(self) -> None:
        self._write_plan(self._plan(alpha_start="09:00"))
        for label, alpha_time, beta_time in (
            ("identity", time(9), time(12)),
            ("worse", time(9, 15), time(10, 15)),
        ):
            with self.subTest(label=label):
                problem, candidate = self._problem_candidate(
                    alpha_time=alpha_time,
                    beta_time=beta_time,
                )
                repository = _RecordingRepository(self._store())
                stager = ScheduleStager(
                    repository,
                    run_id=f"schedule-run-no-progress-{label}",
                )

                review = stager.stage_schedule_candidate(problem, candidate)

                self.assertEqual(ScheduleStageState.REJECTED, review.state)
                self.assertIn(
                    "NO_STRICT_SCHEDULE_IMPROVEMENT",
                    _problem_codes(review),
                )
                self.assertEqual([], repository.preview_calls)

    def test_forged_candidate_is_rejected_before_repository_preview(self) -> None:
        problem, candidate = self._problem_candidate()
        forged = replace(
            candidate,
            candidate_id="sha256:" + ("0" * 64),
        )
        repository = _RecordingRepository(self._store())
        stager = ScheduleStager(repository, run_id="schedule-run-forged")

        review = stager.stage_schedule_candidate(problem, forged)

        self.assertEqual(ScheduleStageState.REJECTED, review.state)
        self.assertIn("CANDIDATE_REPLAY_MISMATCH", _problem_codes(review))
        self.assertEqual([], repository.preview_calls)

    def test_candidate_owned_score_subclass_cannot_bypass_improvement(
        self,
    ) -> None:
        self._write_plan(self._plan(alpha_start="09:00"))
        problem, candidate = self._problem_candidate(
            alpha_time=time(9, 15),
            beta_time=time(10, 15),
        )
        evil = _EvilScore(
            **{
                item.name: getattr(candidate.score, item.name)
                for item in fields(ScheduleScore)
            }
        )
        object.__setattr__(candidate, "score", evil)
        repository = _RecordingRepository(self._store())
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-evil-score",
        )

        review = stager.stage_schedule_candidate(problem, candidate)

        self.assertEqual(ScheduleStageState.REJECTED, review.state)
        self.assertIn("INVALID_CANDIDATE", _problem_codes(review))
        self.assertEqual([], repository.preview_calls)

    def test_unsupported_solver_version_is_rejected_before_preview(self) -> None:
        problem, candidate = self._problem_candidate()
        claimed = replace(candidate, solver="model-claimed-solver")
        repository = _RecordingRepository(self._store())
        stager = ScheduleStager(repository, run_id="schedule-run-solver")

        review = stager.stage_schedule_candidate(problem, claimed)

        self.assertEqual(ScheduleStageState.REJECTED, review.state)
        self.assertIn(
            "UNSUPPORTED_SOLVER_VERSION",
            _problem_codes(review),
        )
        self.assertEqual([], repository.preview_calls)

    def test_stale_problem_without_schedule_receipt_skips_preview(self) -> None:
        problem, candidate = self._problem_candidate()
        current = self._store().load_plan()
        current["generation"] += 1
        current["revision"] = compute_revision(current)
        self._write_plan(current)
        repository = _RecordingRepository(self._store())
        stager = ScheduleStager(repository, run_id="schedule-run-old-problem")

        review = stager.stage_schedule_candidate(problem, candidate)

        self.assertEqual(ScheduleStageState.REJECTED, review.state)
        self.assertIn("STALE_SCHEDULE_PROBLEM", _problem_codes(review))
        self.assertEqual([], repository.preview_calls)
        self.assertEqual([], repository.apply_calls)

    def test_stale_review_and_generation_aba_do_not_implicitly_rebase(self) -> None:
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(self._store(), run_id="schedule-run-stale")
        review = stager.stage_schedule_candidate(problem, candidate)
        self.assertTrue(review.ready_to_commit, review.to_dict())

        current = self._store().load_plan()
        current["generation"] += 1
        current["revision"] = compute_revision(current)
        self._write_plan(current)
        self.assertEqual(
            problem.base_state_digest,
            schedule_problem_from_plan(
                self._store().load_plan(),
                evaluation_at=EVALUATION_AT,
            ).base_state_digest,
        )

        result = stager.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertIn("STALE_SCHEDULE_REVIEW", _problem_codes(result))

    def test_repreview_diff_drift_is_rejected_without_apply(self) -> None:
        repository = _NondeterministicPreviewRepository(self._store())
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(repository, run_id="schedule-run-drift")
        review = stager.stage_schedule_candidate(problem, candidate)

        result = stager.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertIn("PREVIEW_CONTRACT_MISMATCH", _problem_codes(result))
        self.assertEqual([], repository.apply_calls)

    def test_full_store_diff_has_a_hard_change_budget(self) -> None:
        repository = _RecordingRepository(self._store())
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-hard-budget",
            max_changes=1,
            max_auto_changes=1,
        )

        review = stager.stage_schedule_candidate(problem, candidate)

        self.assertEqual(ScheduleStageState.REJECTED, review.state)
        self.assertIn("CHANGE_BUDGET_EXCEEDED", _problem_codes(review))
        self.assertFalse(stager.has_pending_review)
        self.assertEqual(1, len(repository.preview_calls))
        self.assertEqual([], repository.apply_calls)

    def test_preview_contract_rejects_identity_and_effect_tampering(
        self,
    ) -> None:
        for mode in (
            "trip_id",
            "current_revision",
            "draft_digest",
            "state_title",
        ):
            with self.subTest(mode=mode):
                repository = _TamperedPreviewRepository(
                    self._store(),
                    mode=mode,
                )
                problem, candidate = self._problem_candidate()
                stager = ScheduleStager(
                    repository,
                    run_id=f"schedule-run-preview-contract-{mode}",
                )

                review = stager.stage_schedule_candidate(problem, candidate)

                self.assertEqual(ScheduleStageState.REJECTED, review.state)
                self.assertIn(
                    "PREVIEW_CONTRACT_MISMATCH",
                    _problem_codes(review),
                )
                self.assertFalse(stager.has_pending_review)
                self.assertEqual([], repository.apply_calls)

    def test_exact_winner_during_stage_preview_is_replay_confirmed(
        self,
    ) -> None:
        repository = _ExactWinnerOnPreviewRepository(
            self._store(),
            preview_call=1,
        )
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-stage-exact-winner",
        )

        review = stager.stage_schedule_candidate(problem, candidate)

        self.assertEqual(ScheduleStageState.REPLAY_CONFIRMED, review.state)
        self.assertEqual("replayed", review.store_status)
        self.assertFalse(stager.has_pending_review)
        current = self._store().load_plan()
        self.assertIn(f"schedule:{candidate.candidate_id}", current["receipts"])

    def test_exact_winner_during_commit_repreview_is_confirmed(
        self,
    ) -> None:
        repository = _ExactWinnerOnPreviewRepository(
            self._store(),
            preview_call=2,
        )
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-commit-exact-winner",
        )
        review = stager.stage_schedule_candidate(problem, candidate)

        result = stager.commit(review.review_id or "")

        self.assertTrue(result.applied, result.to_dict())
        self.assertTrue(result.replayed)
        self.assertTrue(result.candidate_is_current)
        self.assertEqual(ScheduleStageState.WAITING_EXTERNAL, result.state)
        self.assertFalse(stager.has_pending_review)

    def test_real_rollback_during_commit_repreview_is_typed(self) -> None:
        plan = self._plan(alpha_start="09:00")
        day = plan["state"]["itinerary"]["days"][0]
        day["start_location_id"] = "location-beta"
        day["end_location_id"] = "location-alpha"
        day["places"][0].pop("allowed_windows")
        day["travel"][0]["from_activity_id"] = "activity-beta"
        day["travel"][0]["to_activity_id"] = "activity-alpha"
        plan["revision"] = compute_revision(plan)
        self._write_plan(plan)
        problem = schedule_problem_from_plan(
            self._store().load_plan(),
            evaluation_at=EVALUATION_AT,
        )
        candidate = build_schedule_candidate(
            problem,
            (
                ScheduleAssignment(
                    "activity-beta",
                    "day-1",
                    0,
                    time(9),
                ),
                ScheduleAssignment(
                    "activity-alpha",
                    "day-1",
                    1,
                    time(9, 40),
                ),
            ),
            solver=SOLVER_VERSION,
        )
        repository = _ApplyRollbackOnPreviewRepository(
            self._store(),
            preview_call=2,
        )
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-rollback-race",
        )
        review = stager.stage_schedule_candidate(problem, candidate)
        self.assertTrue(review.ready_to_commit, review.to_dict())

        result = stager.commit(review.review_id or "")

        self.assertFalse(result.applied, result.to_dict())
        self.assertEqual(ScheduleStageState.REJECTED, result.state)
        self.assertIn("SCHEDULE_PATCH_ROLLED_BACK", _problem_codes(result))
        self.assertFalse(stager.has_pending_review)

    def test_apply_cas_race_is_not_reported_as_applied(self) -> None:
        repository = _ExternalWinnerRepository(self._store())
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(repository, run_id="schedule-run-cas")
        review = stager.stage_schedule_candidate(problem, candidate)

        result = stager.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertIn("COMMIT_NOT_CONFIRMED", _problem_codes(result))
        current = self._store().load_plan()
        alpha = current["state"]["itinerary"]["days"][0]["places"][0]
        self.assertEqual("external writer won", alpha["note"])
        self.assertEqual("09:00:00", alpha["time"])

    def test_unrelated_repreview_winner_reports_observed_revision(self) -> None:
        repository = _UnrelatedWinnerOnPreviewRepository(self._store())
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-repreview-winner",
        )
        review = stager.stage_schedule_candidate(problem, candidate)

        result = stager.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertIn("STALE_REVISION", _problem_codes(result))
        observed_revision = self._store().load_plan()["revision"]
        self.assertEqual(observed_revision, result.current_revision)
        self.assertNotEqual(problem.base_revision, result.current_revision)

    def test_fake_applied_ack_without_canonical_write_is_unknown(self) -> None:
        repository = _FakeAppliedRepository(self._store())
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-fake-ack",
        )
        review = stager.stage_schedule_candidate(problem, candidate)
        before = self._store().load_plan()["revision"]

        result = stager.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertFalse(result.candidate_is_current)
        self.assertEqual(ScheduleStageState.OUTCOME_UNKNOWN, result.state)
        self.assertIn("COMMIT_OUTCOME_UNKNOWN", _problem_codes(result))
        self.assertEqual(before, self._store().load_plan()["revision"])
        self.assertTrue(stager.has_pending_review)

        second = stager.commit(review.review_id or "")
        self.assertEqual(ScheduleStageState.OUTCOME_UNKNOWN, second.state)
        self.assertFalse(stager.has_pending_review)
        self.assertEqual(2, len(repository.apply_calls))

        third = stager.commit(review.review_id or "")
        self.assertIn("UNKNOWN_SCHEDULE_REVIEW", _problem_codes(third))
        self.assertEqual(2, len(repository.apply_calls))

    def test_raise_after_durable_apply_recovers_with_exact_retry(self) -> None:
        repository = _RaiseAfterDurableApplyRepository(self._store())
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-raise-after-apply",
        )
        review = stager.stage_schedule_candidate(problem, candidate)

        result = stager.commit(review.review_id or "")

        self.assertTrue(result.applied, result.to_dict())
        self.assertTrue(result.replayed)
        self.assertTrue(result.candidate_is_current)
        self.assertEqual(2, len(repository.apply_calls))
        self.assertIs(
            repository.apply_calls[0][0],
            repository.apply_calls[1][0],
        )

    def test_commit_outcome_unknown_retries_the_same_patch_once(self) -> None:
        unknown = StoreResult(
            success=False,
            status="commit_outcome_unknown",
            action="patch",
        )
        repository = _ScriptedApplyRepository(
            self._store(),
            (unknown, _DELEGATE),
        )
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(repository, run_id="schedule-run-recovery")
        review = stager.stage_schedule_candidate(problem, candidate)

        result = stager.commit(review.review_id or "")

        self.assertTrue(result.applied, result.to_dict())
        self.assertEqual(2, len(repository.apply_calls))
        self.assertIs(
            repository.apply_calls[0][0],
            repository.apply_calls[1][0],
        )

    def test_double_outcome_unknown_stops_exact_review(self) -> None:
        unknown = StoreResult(
            success=False,
            status="commit_outcome_unknown",
            action="patch",
        )
        repository = _ScriptedApplyRepository(
            self._store(),
            (unknown, unknown),
        )
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(repository, run_id="schedule-run-unknown")
        review = stager.stage_schedule_candidate(problem, candidate)

        result = stager.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertEqual(ScheduleStageState.OUTCOME_UNKNOWN, result.state)
        self.assertIn("COMMIT_OUTCOME_UNKNOWN", _problem_codes(result))
        self.assertEqual(2, len(repository.apply_calls))
        self.assertIs(
            repository.apply_calls[0][0],
            repository.apply_calls[1][0],
        )
        again = stager.commit(review.review_id or "")
        self.assertIn("UNKNOWN_SCHEDULE_REVIEW", _problem_codes(again))
        self.assertEqual(2, len(repository.apply_calls))

    def test_unsubstantiated_rolled_back_ack_is_not_confirmed(self) -> None:
        rolled_back = StoreResult(
            success=True,
            status="replayed_rolled_back",
            action="patch",
            replayed=True,
        )
        repository = _ScriptedApplyRepository(
            self._store(),
            (rolled_back,),
        )
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(repository, run_id="schedule-run-rolled-back")
        review = stager.stage_schedule_candidate(problem, candidate)

        result = stager.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertEqual(ScheduleStageState.OUTCOME_UNKNOWN, result.state)
        self.assertIn("COMMIT_OUTCOME_UNKNOWN", _problem_codes(result))
        self.assertTrue(stager.has_pending_review)
        self.assertEqual(1, len(repository.apply_calls))

    def test_real_after_replace_fault_recovers_from_exact_receipt(self) -> None:
        def fail_after_replace(stage: str) -> None:
            if stage == "after_replace":
                raise RuntimeError("after replace")

        faulting_store = TripStore(
            self.trips_root,
            self.slug,
            fault_hook=fail_after_replace,
        )
        repository = _RecordingRepository(faulting_store)
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-real-receipt",
        )
        review = stager.stage_schedule_candidate(problem, candidate)

        result = stager.commit(review.review_id or "")

        self.assertTrue(result.applied, result.to_dict())
        self.assertTrue(result.replayed)
        self.assertEqual("replayed", result.store_status)
        self.assertEqual(ScheduleStageState.WAITING_EXTERNAL, result.state)
        self.assertEqual(review.changes, result.changes)
        self.assertEqual(
            review.invalidated_day_ids,
            result.invalidated_day_ids,
        )
        self.assertEqual(2, len(repository.apply_calls))
        self.assertIs(
            repository.apply_calls[0][0],
            repository.apply_calls[1][0],
        )
        current = self._store().load_plan()
        self.assertIn(
            f"schedule:{candidate.candidate_id}",
            current["receipts"],
        )

    def test_exact_receipt_replay_recovers_after_external_apply(self) -> None:
        repository = _RecordingRepository(self._store())
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(repository, run_id="schedule-run-replay")
        review = stager.stage_schedule_candidate(problem, candidate)
        patch = repository.preview_calls[0][0]
        externally_applied = self._store().apply_patch(
            patch,
            evaluation_at=EVALUATION_AT,
        )
        self.assertTrue(externally_applied.success, externally_applied.to_dict())

        result = stager.commit(review.review_id or "")

        self.assertTrue(result.applied, result.to_dict())
        self.assertTrue(result.replayed)
        self.assertEqual(ScheduleStageState.WAITING_EXTERNAL, result.state)
        self.assertTrue(result.candidate_is_current)

    def test_receipt_reconciliation_read_failure_is_not_false_stale(
        self,
    ) -> None:
        repository = _FailNthLoadRepository(
            self._store(),
            fail_on=3,
        )
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-receipt-read-failure",
        )
        review = stager.stage_schedule_candidate(problem, candidate)
        patch = repository.preview_calls[0][0]
        externally_applied = self._store().apply_patch(
            patch,
            evaluation_at=EVALUATION_AT,
        )
        self.assertTrue(externally_applied.success, externally_applied.to_dict())

        result = stager.commit(review.review_id or "")

        self.assertFalse(result.applied)
        self.assertEqual(ScheduleStageState.OUTCOME_UNKNOWN, result.state)
        self.assertIn("COMMIT_OUTCOME_UNKNOWN", _problem_codes(result))
        self.assertNotIn("STALE_SCHEDULE_REVIEW", _problem_codes(result))
        self.assertTrue(stager.has_pending_review)

    def test_receipt_replay_does_not_claim_an_advanced_state_is_current(
        self,
    ) -> None:
        repository = _RecordingRepository(self._store())
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            repository,
            run_id="schedule-run-historical-replay",
        )
        review = stager.stage_schedule_candidate(problem, candidate)
        patch = repository.preview_calls[0][0]
        applied = self._store().apply_patch(
            patch,
            evaluation_at=EVALUATION_AT,
        )
        self.assertTrue(applied.success, applied.to_dict())
        advanced = self._external_note("after-schedule-note")
        self.assertTrue(advanced.success, advanced.to_dict())

        result = stager.commit(review.review_id or "")

        self.assertTrue(result.applied, result.to_dict())
        self.assertTrue(result.replayed)
        self.assertFalse(result.candidate_is_current)
        self.assertEqual(
            ScheduleStageState.REPLAY_CONFIRMED,
            result.state,
        )
        self.assertIsNone(result.check_report)
        self.assertNotEqual(
            result.applied_revision,
            result.current_revision,
        )

    def test_human_checkpoint_and_store_approval_are_independent(self) -> None:
        self._write_plan(self._plan(protected=True))
        problem, candidate = self._problem_candidate()
        stager = ScheduleStager(
            self._store(),
            run_id="schedule-run-approvals",
            max_auto_changes=0,
        )
        review = stager.stage_schedule_candidate(problem, candidate)

        self.assertEqual(ScheduleStageState.WAITING_APPROVAL, review.state)
        self.assertIn("LARGE_PERSISTENT_DIFF", review.risk_codes)
        self.assertIsNotNone(review.required_approval_scope)
        self.assertTrue(review.protected_changes)
        self.assertTrue(review.to_dict()["protected_changes"])
        store_grant = ApprovalGrant(
            approval_id="schedule-store-approval",
            scope_digest=review.required_approval_scope,
            approved_by="fixture-human",
            approved_at=EVALUATION_AT.isoformat(),
        )
        human_grant = HumanCheckpointGrant(
            review_id=review.review_id or "",
            approved_by="fixture-human",
            approved_at=EVALUATION_AT,
        )

        no_human = stager.commit(
            review.review_id or "",
            approvals=(store_grant,),
        )
        self.assertIn("HUMAN_CHECKPOINT_REQUIRED", _problem_codes(no_human))

        no_store = stager.commit(
            review.review_id or "",
            human_grant=human_grant,
        )
        self.assertIn("STORE_APPROVAL_REQUIRED", _problem_codes(no_store))

        applied = stager.commit(
            review.review_id or "",
            human_grant=human_grant,
            approvals=(store_grant,),
        )
        self.assertTrue(applied.applied, applied.to_dict())
        self.assertEqual(
            review.protected_changes,
            applied.protected_changes,
        )

    def test_public_stage_and_commit_values_reject_impossible_states(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            ScheduleStageReview(
                state=ScheduleStageState.READY,
                problem_id="problem",
                candidate_id="candidate",
                base_revision="revision",
            )
        with self.assertRaises(ValueError):
            ScheduleStageReview(
                state=ScheduleStageState.WAITING_APPROVAL,
                problem_id="problem",
                candidate_id="candidate",
                base_revision="revision",
                review_id="review",
                patch_digest="digest",
            )
        problem = ScheduleStageProblem("TEST_PROBLEM", "test problem")
        with self.assertRaises(ValueError):
            ScheduleCommitResult(
                state=ScheduleStageState.APPLIED,
                applied=False,
            )
        with self.assertRaises(ValueError):
            ScheduleCommitResult(
                state=ScheduleStageState.APPLIED,
                applied=True,
            )
        with self.assertRaises(ValueError):
            ScheduleCommitResult(
                state=ScheduleStageState.WAITING_EXTERNAL,
                applied=True,
            )
        with self.assertRaises(ValueError):
            ScheduleCommitResult(
                state=ScheduleStageState.REPLAY_CONFIRMED,
                applied=True,
            )
        with self.assertRaises(ValueError):
            ScheduleCommitResult(
                state=ScheduleStageState.REJECTED,
                applied=True,
                problems=(problem,),
            )
        with self.assertRaises(ValueError):
            ScheduleCommitResult(
                state=ScheduleStageState.OUTCOME_UNKNOWN,
                applied=False,
            )
        with self.assertRaises(ValueError):
            ScheduleCommitResult(
                state=ScheduleStageState.WAITING_APPROVAL,
                applied=False,
                problems=(problem,),
            )


if __name__ == "__main__":
    unittest.main()
