"""Adversarial offline tests for the Phase 2 AI repair contract boundary.

The audited catalogs below mirror values emitted by ``loaders.py`` and
``timeline.py``.  Keeping that inventory explicit makes a new kernel issue or
suggested fix fail review until its repair ownership is deliberately assigned.
"""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from trip_planner.codec import build_plan
from trip_planner.composition import EvidenceBinding
from trip_planner.facts import (
    EvidenceLedger,
    EvidencePersistence,
    EvidenceSnapshot,
    FactKind,
    ProviderPolicy,
    ProviderPolicyRegistry,
)
from trip_planner.models import (
    CheckIssue,
    CheckReport,
    CheckStatus,
    IssueSeverity,
)
from trip_planner.mutations import (
    AddActivity,
    AddConstraint,
    PlaceActivity,
    RemoveActivity,
    RemoveConstraint,
    UpdateActivity,
    UpdateConstraint,
    UpdateDay,
    patch_to_dict,
)
from trip_planner.repair import (
    FIX_REGISTRY_KEYS,
    KNOWN_ISSUE_CODES,
    PROPOSAL_VERSION,
    SNAPSHOT_VERSION,
    IssueOwner,
    OperationReason,
    PlannerSnapshot,
    ProposalIntent,
    RepairBudget,
    RepairContractError,
    RepairOptionKind,
    attempt_digest,
    bind_proposal,
    build_repair_issues,
    decode_proposal_intent,
    effect_digest,
    issue_id_for,
    operation_matches_option,
    options_for_fix,
    semantic_state_digest,
    snapshot_from_plan,
)


_AUDITED_EMITTED_ISSUE_CODES = frozenset(
    {
        "ACTIVITY_DURATION_UNVERIFIED",
        "ALLOWED_DAY_VIOLATION",
        "AMBIGUOUS_LOCAL_TIME",
        "AT_MOST_ONCE_VIOLATION",
        "CHOOSE_N_VIOLATION",
        "CONFLICTED_EVIDENCE",
        "CONSTRAINT_NEEDS_VERIFICATION",
        "DAILY_LIMIT_EXCEEDED",
        "DATE_RANGE_MISSING",
        "DAY_OUTSIDE_TRIP_RANGE",
        "DAY_TIMEZONE_INVALID",
        "DAY_WINDOW_VIOLATION",
        "DISALLOWED_MODE",
        "EXACTLY_ONCE_VIOLATION",
        "FIXED_TIME_CONFLICT",
        "FRESHNESS_NOT_EVALUATED",
        "GLOBAL_TIMELINE_OVERLAP",
        "INBOUND_TIMELINE_OVERLAP",
        "INVALID_CONSTRAINT",
        "INVALID_FRESHNESS_TIMESTAMP",
        "INVALID_TIMEZONE",
        "INVALID_TRAVEL_REFERENCE",
        "LOCATION_CONTINUITY_VIOLATION",
        "MISSING_DAY_BOUNDS",
        "MISSING_DURATION",
        "MISSING_END_LOCATION",
        "MISSING_REQUIRED_ACTIVITY",
        "MISSING_START_LOCATION",
        "MISSING_TRAVEL_ESTIMATE",
        "NONEXISTENT_LOCAL_TIME",
        "POSSIBLE_DAILY_LIMIT_EXCEEDED",
        "POSSIBLE_DAY_WINDOW_VIOLATION",
        "POSSIBLE_FIXED_TIME_CONFLICT",
        "POSSIBLE_GLOBAL_TIMELINE_OVERLAP",
        "POSSIBLE_INBOUND_TIMELINE_OVERLAP",
        "POSSIBLE_PRECEDENCE_VIOLATION",
        "POSSIBLE_RETURN_AFTER_DAY_END",
        "POSSIBLE_RETURN_TIMELINE_OVERLAP",
        "POSSIBLE_SCHEDULED_START_CONFLICT",
        "POSSIBLE_TIME_WINDOW_VIOLATION",
        "PRECEDENCE_SUBJECT_MISSING",
        "PRECEDENCE_VIOLATION",
        "RECOMMENDED_MODE_UNAVAILABLE",
        "REQUIRES_VIOLATION",
        "RETURN_AFTER_DAY_END",
        "RETURN_TIMELINE_OVERLAP",
        "SCHEDULED_START_CONFLICT",
        "SCHEDULE_DATE_ROLLOVER_INFERRED",
        "STALE_EVIDENCE",
        "SYNTHETIC_ACTIVITY_IDS",
        "TIMEZONE_FALLBACK",
        "TIME_WINDOW_VIOLATION",
        "TRAVEL_EVIDENCE_UNVERIFIED",
        "UNSCHEDULED_SELECTED_ACTIVITY",
        "UNVERIFIED_EVIDENCE",
    }
)

_AUDITED_EMITTED_FIX_KEYS = frozenset(
    {
        "add_interday_transfer",
        "align_day_bases",
        "assign_activity_to_day",
        "change_decision_state",
        "change_inbound_route",
        "change_previous_activity",
        "change_return_route",
        "change_route",
        "change_selected_choices",
        "choose_allowed_travel_mode",
        "estimate_or_verify_activity_durations",
        "evaluate_with_as_of_time",
        "extend_day_availability",
        "fetch_travel_estimate",
        "keep_one_variant",
        "move_activity",
        "move_activity_outside_dst_gap",
        "move_activity_to_allowed_day",
        "move_activity_to_another_day",
        "move_last_activity",
        "persist_stable_activity_ids",
        "rebuild_day_travel_edges",
        "reconcile_trip_and_day_dates",
        "refresh_opening_hours",
        "refresh_travel_estimate",
        "refresh_travel_estimates",
        "remove_trigger_activity",
        "reorder_activities",
        "repair_constraint",
        "resolve_travel_evidence",
        "restore_fixed_time",
        "schedule_constraint_subjects",
        "schedule_dependencies",
        "schedule_interday_transfer",
        "schedule_required_activity",
        "select_exactly_one_variant",
        "set_activity_duration",
        "set_day_availability",
        "set_day_base_locations",
        "set_day_end_location",
        "set_day_iana_timezone",
        "set_day_start_location",
        "set_explicit_activity_datetime",
        "set_trip_date_range",
        "set_trip_iana_timezone",
        "set_valid_iana_timezone",
        "shorten_activity",
        "specify_dst_fold",
        "verify_activity_durations",
        "verify_activity_fact",
        "verify_timing_evidence",
        "verify_travel_estimate",
    }
)

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
_EVIDENCE_AT = datetime(2026, 7, 27, 9, 30, tzinfo=timezone.utc)


def _evidence_snapshot(generation: int) -> EvidenceSnapshot:
    ledger = EvidenceLedger(
        _EVIDENCE_POLICIES,
        generation=generation,
    )
    return EvidenceSnapshot.from_ledger(
        ledger,
        evaluation_at=_EVIDENCE_AT,
        purge_now=_EVIDENCE_AT,
    )


def _issue(
    *,
    code: str = "MISSING_DURATION",
    severity: IssueSeverity = IssueSeverity.WARNING,
    message: str = "A duration is missing.",
    activity_ids: tuple[str, ...] = ("activity-1",),
    evidence_refs: tuple[str, ...] = (),
    details: tuple[tuple[str, str | int | float | bool | None], ...] = (
        ("day_id", "day-1"),
    ),
    fixes: tuple[str, ...] = ("set_activity_duration",),
) -> CheckIssue:
    return CheckIssue(
        code=code,
        severity=severity,
        message=message,
        activity_ids=activity_ids,
        evidence_refs=evidence_refs,
        details=details,
        suggested_fixes=fixes,
    )


def _snapshot_with_issue(issue: CheckIssue | None = None) -> PlannerSnapshot:
    report = CheckReport(
        status=CheckStatus.NEEDS_VERIFICATION,
        issues=(issue or _issue(),),
    )
    issues = build_repair_issues(report)
    state = {
        "trip": {"slug": "contract-trip"},
        "itinerary": {"days": []},
    }
    return PlannerSnapshot(
        contract_version=SNAPSHOT_VERSION,
        snapshot_id=f"snapshot-{'a' * 64}",
        trip_id="contract-trip",
        revision="b" * 64,
        evaluation_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        state_digest=semantic_state_digest(state),
        state=state,
        report=report,
        issues=issues,
        remaining_iterations=4,
        remaining_provider_calls=5,
        remaining_changes=10,
    )


def _canonical_plan(
    *,
    generation: int = 1,
    receipts: dict[str, Any] | None = None,
    title: str = "Contract trip",
) -> dict[str, Any]:
    return build_plan(
        trip_id="contract-trip",
        generation=generation,
        state={
            "trip": {
                "slug": "contract-trip",
                "title": title,
                "timezone": "Asia/Taipei",
                "date_range": "2026-10-01 ~ 2026-10-01",
                "cities": ["Fixture City"],
            },
            "itinerary": {
                "available_modes": ["walking"],
                "days": [
                    {
                        "day_id": "day-1",
                        "day": 1,
                        "date": "2026-10-01",
                        "timezone": "Asia/Taipei",
                        "available_start": "08:00",
                        "available_end": "20:00",
                        "start_location_id": "base",
                        "end_location_id": "base",
                        "places": [],
                        "travel": [],
                    }
                ],
            },
        },
        receipts=receipts,
    )


def _proposal_payload(
    operations: list[dict[str, Any]],
    *,
    reasons: list[dict[str, Any]] | None = None,
    summary: str = "Repair the cited issues.",
) -> dict[str, Any]:
    if reasons is None:
        reasons = [
            {
                "op_id": operation["op_id"],
                "issue_ids": [f"issue-{'1' * 32}"],
                "option_key": "set_activity_duration",
                "reason": (
                    "A" * 300
                    if index == 0
                    else f"Reason for {operation['op_id']}"
                ),
            }
            for index, operation in enumerate(operations)
        ]
    return {
        "proposal_version": PROPOSAL_VERSION,
        "summary": summary,
        "operations": operations,
        "reasons": reasons,
    }


class Phase2RepairRegistryTests(unittest.TestCase):
    def test_registry_covers_current_kernel_issue_and_fix_catalogs(self) -> None:
        self.assertEqual(len(_AUDITED_EMITTED_ISSUE_CODES), 55)
        self.assertEqual(KNOWN_ISSUE_CODES, _AUDITED_EMITTED_ISSUE_CODES)
        self.assertEqual(len(_AUDITED_EMITTED_FIX_KEYS), 52)
        self.assertEqual(FIX_REGISTRY_KEYS, _AUDITED_EMITTED_FIX_KEYS)

        for fix in sorted(_AUDITED_EMITTED_FIX_KEYS):
            with self.subTest(fix=fix):
                options = options_for_fix(fix)
                self.assertTrue(options)
                self.assertFalse(any(option.blocking for option in options))
                self.assertTrue(all(option.source_fix in (None, fix) for option in options))

    def test_unknown_issue_and_fix_fail_closed_to_system_manual_review(self) -> None:
        unknown_fix = options_for_fix("future_unreviewed_fix")
        self.assertEqual(len(unknown_fix), 1)
        option = unknown_fix[0]
        self.assertEqual(option.key, "manual_review")
        self.assertIs(option.kind, RepairOptionKind.SYSTEM_ACTION)
        self.assertIs(option.owner, IssueOwner.SYSTEM)
        self.assertFalse(option.auto_allowed)
        self.assertTrue(option.blocking)
        self.assertEqual(option.source_fix, "future_unreviewed_fix")

        unknown_issue = _issue(
            code="FUTURE_UNREVIEWED_ISSUE",
            fixes=("set_activity_duration",),
        )
        result = build_repair_issues(
            CheckReport(
                status=CheckStatus.NEEDS_VERIFICATION,
                issues=(unknown_issue,),
            )
        )
        self.assertEqual(len(result), 1)
        self.assertIs(result[0].owner, IssueOwner.SYSTEM)
        self.assertFalse(result[0].auto_repairable)
        self.assertEqual(result[0].options[0].key, "manual_review")
        self.assertTrue(result[0].options[0].blocking)
        self.assertEqual(
            result[0].options[0].source_fix,
            "FUTURE_UNREVIEWED_ISSUE",
        )

        known_issue_unknown_fix = _issue(fixes=("future_unreviewed_fix",))
        result = build_repair_issues(
            CheckReport(
                status=CheckStatus.NEEDS_VERIFICATION,
                issues=(known_issue_unknown_fix,),
            )
        )
        self.assertFalse(result[0].auto_repairable)
        self.assertTrue(result[0].options[0].blocking)


class Phase2RepairIssueIdentityTests(unittest.TestCase):
    def test_issue_id_ignores_presentation_but_keeps_structural_scope(self) -> None:
        base = _issue(
            code="LOCATION_CONTINUITY_VIOLATION",
            severity=IssueSeverity.ERROR,
            message="Old wording.",
            activity_ids=(),
            evidence_refs=("evidence-b", "evidence-a"),
            details=(
                ("previous_day_id", "day-1"),
                ("following_day_id", "day-2"),
                ("previous_end_location_id", "hotel-a"),
                ("following_start_location_id", "hotel-b"),
            ),
            fixes=("add_interday_transfer",),
        )
        presentation_only = _issue(
            code="LOCATION_CONTINUITY_VIOLATION",
            severity=IssueSeverity.WARNING,
            message="New translated wording.",
            activity_ids=(),
            evidence_refs=("evidence-a", "evidence-b"),
            details=base.details,
            fixes=("align_day_bases", "add_interday_transfer"),
        )
        different_pair = _issue(
            code="LOCATION_CONTINUITY_VIOLATION",
            severity=IssueSeverity.ERROR,
            message=base.message,
            activity_ids=(),
            evidence_refs=base.evidence_refs,
            details=(
                ("previous_day_id", "day-2"),
                ("following_day_id", "day-3"),
                ("previous_end_location_id", "hotel-b"),
                ("following_start_location_id", "hotel-c"),
            ),
            fixes=base.suggested_fixes,
        )
        different_location_pair = _issue(
            code=base.code,
            severity=base.severity,
            message=base.message,
            activity_ids=base.activity_ids,
            evidence_refs=base.evidence_refs,
            details=(
                ("previous_day_id", "day-1"),
                ("following_day_id", "day-2"),
                ("previous_end_location_id", "hotel-x"),
                ("following_start_location_id", "hotel-b"),
            ),
            fixes=base.suggested_fixes,
        )

        self.assertEqual(issue_id_for(base), issue_id_for(presentation_only))
        self.assertNotEqual(issue_id_for(base), issue_id_for(different_pair))
        self.assertNotEqual(
            issue_id_for(base),
            issue_id_for(different_location_pair),
        )

        reversed_activities = _issue(
            code="GLOBAL_TIMELINE_OVERLAP",
            severity=IssueSeverity.ERROR,
            message="The same overlap with reversed presentation order.",
            activity_ids=("activity-2", "activity-1"),
            details=(
                ("first_day_id", "day-1"),
                ("second_day_id", "day-2"),
            ),
            fixes=("move_activity",),
        )
        forward_activities = _issue(
            code=reversed_activities.code,
            severity=reversed_activities.severity,
            message=reversed_activities.message,
            activity_ids=tuple(reversed(reversed_activities.activity_ids)),
            details=reversed_activities.details,
            fixes=reversed_activities.suggested_fixes,
        )
        self.assertEqual(
            issue_id_for(forward_activities),
            issue_id_for(reversed_activities),
        )

        invalid_day_one = _issue(
            code="DAY_TIMEZONE_INVALID",
            activity_ids=(),
            details=(("day_id", "day-1"), ("fallback", "UTC")),
            fixes=("set_day_iana_timezone",),
        )
        invalid_day_two = _issue(
            code=invalid_day_one.code,
            activity_ids=(),
            details=(("day_id", "day-2"), ("fallback", "UTC")),
            fixes=invalid_day_one.suggested_fixes,
        )
        self.assertNotEqual(
            issue_id_for(invalid_day_one),
            issue_id_for(invalid_day_two),
        )

    def test_possible_and_definite_variants_merge_into_one_family_issue(self) -> None:
        possible = _issue(
            code="POSSIBLE_PRECEDENCE_VIOLATION",
            severity=IssueSeverity.WARNING,
            message="Timing might violate precedence.",
            activity_ids=("activity-1", "activity-2"),
            details=(("constraint_id", "constraint-1"), ("strength", "hard")),
            fixes=("verify_timing_evidence", "reorder_activities"),
        )
        definite = _issue(
            code="PRECEDENCE_VIOLATION",
            severity=IssueSeverity.ERROR,
            message="Timing violates precedence.",
            activity_ids=possible.activity_ids,
            details=possible.details,
            fixes=("reorder_activities",),
        )

        self.assertEqual(issue_id_for(possible), issue_id_for(definite))
        issues = build_repair_issues(
            CheckReport(
                status=CheckStatus.INFEASIBLE,
                issues=(possible, definite),
            )
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].family, "PRECEDENCE_VIOLATION")
        self.assertEqual(issues[0].check.code, "PRECEDENCE_VIOLATION")
        self.assertEqual(
            {option.key for option in issues[0].options},
            {"verify_timing_evidence", "reorder_activities"},
        )


class Phase2RepairSnapshotTests(unittest.TestCase):
    def test_zero_budgets_support_inspection_only_runs(self) -> None:
        budget = RepairBudget(
            max_iterations=0,
            max_provider_calls=0,
            max_changes=0,
            max_auto_changes_per_patch=0,
        )
        self.assertEqual(0, budget.max_iterations)
        self.assertEqual(0, budget.max_provider_calls)
        self.assertEqual(0, budget.max_changes)

    def test_snapshot_is_model_facing_and_excludes_store_internals(self) -> None:
        receipt = {
            "kind": "patch",
            "status": "applied",
            "request_digest": f"sha256:{'1' * 64}",
            "transaction_id": f"tx-{'2' * 32}",
            "base_revision": "3" * 64,
            "applied_revision": "4" * 64,
            "applied_generation": 1,
        }
        plan = _canonical_plan(receipts={"repair-request": receipt})
        snapshot = snapshot_from_plan(
            plan,
            evaluation_at=datetime(
                2026, 7, 27, 9, 30, tzinfo=timezone.utc
            ),
            remaining_iterations=3,
            remaining_provider_calls=4,
            remaining_changes=9,
        )
        payload = snapshot.to_dict()

        self.assertEqual(
            set(payload),
            {
                "contract_version",
                "snapshot_id",
                "trip_id",
                "revision",
                "evaluation_at",
                "state_digest",
                "composed_state_digest",
                "report_digest",
                "decision_context_digest",
                "evidence_binding",
                "state",
                "report",
                "issues",
                "budget",
            },
        )
        self.assertNotIn("generation", payload)
        self.assertNotIn("receipts", payload)
        self.assertNotIn("path", payload)
        self.assertNotIn("history", payload)
        self.assertNotIn("lock", payload)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("repair-request", serialized)
        self.assertNotIn("transaction_id", serialized)
        self.assertNotIn("filesystem", serialized)

    def test_evidence_binding_changes_only_the_decision_context(self) -> None:
        plan = _canonical_plan()
        first = snapshot_from_plan(
            plan,
            evaluation_at=_EVIDENCE_AT,
            remaining_iterations=3,
            remaining_provider_calls=4,
            remaining_changes=9,
            evidence_snapshot=_evidence_snapshot(0),
        )
        refreshed = snapshot_from_plan(
            plan,
            evaluation_at=_EVIDENCE_AT,
            remaining_iterations=3,
            remaining_provider_calls=4,
            remaining_changes=9,
            evidence_snapshot=_evidence_snapshot(1),
        )

        self.assertEqual(first.state, refreshed.state)
        self.assertEqual(first.state_digest, refreshed.state_digest)
        self.assertEqual(
            first.composed_state_digest,
            refreshed.composed_state_digest,
        )
        self.assertEqual(first.report_digest, refreshed.report_digest)
        self.assertNotEqual(
            first.decision_context_digest,
            refreshed.decision_context_digest,
        )
        self.assertNotEqual(first.snapshot_id, refreshed.snapshot_id)
        self.assertIsNotNone(first.evidence_binding)
        payload = first.to_dict()
        self.assertEqual(
            first.evidence_binding.to_dict(),
            payload["evidence_binding"],
        )
        self.assertEqual(
            {
                "status",
                "report_digest",
                "issue_count",
                "timeline_entry_count",
                "day_summary_count",
            },
            set(payload["report"]),
        )
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("live_attributions", serialized)
        self.assertNotIn("EvidenceSnapshot", repr(first))

    def test_evidence_bound_snapshot_redacts_provider_derived_report_values(
        self,
    ) -> None:
        marker = "provider-derived-secret-marker"
        snapshot = _snapshot_with_issue(
            _issue(
                message=marker,
                details=(("provider_value", marker),),
            )
        )
        evidence_bound = replace(
            snapshot,
            decision_context_digest="",
            evidence_binding=EvidenceBinding(
                policy_registry_revision="1" * 64,
                store_revision="2" * 64,
                evidence_revision="3" * 64,
                evaluation_at=snapshot.evaluation_at,
                purge_checked_at=snapshot.evaluation_at,
                snapshot_id="4" * 64,
            ),
        )

        serialized = json.dumps(evidence_bound.to_dict(), sort_keys=True)

        self.assertNotIn(marker, serialized)
        self.assertNotIn(marker, repr(evidence_bound))

    def test_semantic_digest_ignores_generation_and_receipts_but_tracks_state(self) -> None:
        first = _canonical_plan(
            generation=1,
            receipts={
                "one": {
                    "kind": "patch",
                    "status": "applied",
                    "request_digest": f"sha256:{'1' * 64}",
                    "transaction_id": f"tx-{'2' * 32}",
                    "base_revision": "3" * 64,
                    "applied_revision": "4" * 64,
                    "applied_generation": 1,
                }
            },
        )
        second = _canonical_plan(generation=7, receipts={})
        changed = _canonical_plan(generation=1, title="Different title")

        self.assertNotEqual(first["revision"], second["revision"])
        self.assertEqual(
            semantic_state_digest(first["state"]),
            semantic_state_digest(second["state"]),
        )
        self.assertNotEqual(
            semantic_state_digest(first["state"]),
            semantic_state_digest(changed["state"]),
        )

        evaluation_at = datetime(2026, 7, 27, tzinfo=timezone.utc)
        first_snapshot = snapshot_from_plan(
            first,
            evaluation_at=evaluation_at,
            remaining_iterations=1,
            remaining_provider_calls=1,
            remaining_changes=1,
        )
        second_snapshot = snapshot_from_plan(
            second,
            evaluation_at=evaluation_at,
            remaining_iterations=1,
            remaining_provider_calls=1,
            remaining_changes=1,
        )
        self.assertEqual(first_snapshot.state_digest, second_snapshot.state_digest)
        self.assertEqual(
            first_snapshot.decision_context_digest,
            second_snapshot.decision_context_digest,
        )
        effect = f"effect-{'a' * 64}"
        self.assertEqual(
            attempt_digest(first_snapshot.decision_context_digest, effect),
            attempt_digest(second_snapshot.decision_context_digest, effect),
        )

        budget_changed = snapshot_from_plan(
            first,
            evaluation_at=evaluation_at,
            remaining_iterations=0,
            remaining_provider_calls=1,
            remaining_changes=1,
        )
        self.assertEqual(first_snapshot.state_digest, budget_changed.state_digest)
        self.assertNotEqual(first_snapshot.snapshot_id, budget_changed.snapshot_id)


class Phase2RepairProposalTests(unittest.TestCase):
    def test_option_operation_policy_distinguishes_move_from_time_write(self) -> None:
        plain_move = PlaceActivity(
            "move",
            "activity-1",
            "day-2",
        )
        timed_move = PlaceActivity(
            "move",
            "activity-1",
            "day-2",
            scheduled_start="10:30",
        )
        self.assertTrue(operation_matches_option("move_activity", plain_move))
        self.assertFalse(operation_matches_option("move_activity", timed_move))
        self.assertTrue(
            operation_matches_option(
                "move_activity_outside_dst_gap",
                timed_move,
            )
        )

    def test_effect_digest_ignores_operation_ids_and_reason_text(self) -> None:
        snapshot = _snapshot_with_issue()
        issue_id = snapshot.issues[0].issue_id
        first_operations = (
            UpdateActivity("model-op-a", "activity-1", {"duration_min": 45}),
            UpdateDay("model-op-b", "day-1", {"available_end": "21:00"}),
        )
        renamed_operations = (
            UpdateActivity("renamed-a", "activity-1", {"duration_min": 45}),
            UpdateDay("renamed-b", "day-1", {"available_end": "21:00"}),
        )
        self.assertEqual(
            effect_digest(first_operations),
            effect_digest(renamed_operations),
        )
        self.assertNotEqual(
            effect_digest(first_operations),
            effect_digest(tuple(reversed(first_operations))),
        )
        self.assertNotEqual(
            effect_digest(first_operations),
            effect_digest(
                (
                    UpdateActivity(
                        "model-op-a",
                        "activity-1",
                        {"duration_min": 60},
                    ),
                    first_operations[1],
                )
            ),
        )

        first = ProposalIntent(
            operations=first_operations,
            reasons=(
                OperationReason(
                    "model-op-a",
                    (issue_id,),
                    "set_activity_duration",
                    "First wording.",
                ),
                OperationReason(
                    "model-op-b",
                    (issue_id,),
                    "set_activity_duration",
                    "Second wording.",
                ),
            ),
        )
        renamed = ProposalIntent(
            operations=renamed_operations,
            reasons=(
                OperationReason(
                    "renamed-a",
                    (issue_id,),
                    "set_activity_duration",
                    "Translated wording.",
                ),
                OperationReason(
                    "renamed-b",
                    (issue_id,),
                    "set_activity_duration",
                    "Another explanation.",
                ),
            ),
        )
        first_bound = bind_proposal(snapshot, first, run_id="repair-run")
        renamed_bound = bind_proposal(snapshot, renamed, run_id="repair-run")
        self.assertEqual(first_bound.effect_digest, renamed_bound.effect_digest)
        self.assertEqual(first_bound.attempt_digest, renamed_bound.attempt_digest)
        self.assertEqual(
            first_bound.patch.idempotency_key,
            renamed_bound.patch.idempotency_key,
        )
        renamed_summary = ProposalIntent(
            operations=renamed.operations,
            reasons=renamed.reasons,
            summary="A materially different stored intent.",
        )
        self.assertNotEqual(
            first_bound.patch.idempotency_key,
            bind_proposal(
                snapshot,
                renamed_summary,
                run_id="repair-run",
            ).patch.idempotency_key,
        )

    def test_bind_injects_trusted_identity_and_rejects_unknown_issue(self) -> None:
        snapshot = _snapshot_with_issue()
        issue_id = snapshot.issues[0].issue_id
        proposal = ProposalIntent(
            operations=(
                UpdateActivity(
                    "set-duration",
                    "activity-1",
                    {"duration_min": 45},
                ),
            ),
            reasons=(
                OperationReason(
                    "set-duration",
                    (issue_id,),
                    "set_activity_duration",
                    "Resolve the missing duration.",
                ),
            ),
            summary="Set the missing duration.",
        )
        bound = bind_proposal(snapshot, proposal, run_id="repair-run")

        self.assertEqual(bound.snapshot_id, snapshot.snapshot_id)
        self.assertEqual(bound.patch.trip_id, snapshot.trip_id)
        self.assertEqual(bound.patch.base_revision, snapshot.revision)
        self.assertTrue(bound.patch.idempotency_key.startswith("repair-"))
        self.assertEqual(bound.patch.intent, proposal.summary)
        patch_payload = patch_to_dict(bound.patch)
        self.assertNotIn("approval", patch_payload)
        self.assertNotIn("status", patch_payload)
        self.assertEqual(
            bound.patch.idempotency_key,
            bind_proposal(
                snapshot,
                proposal,
                run_id="repair-run",
            ).patch.idempotency_key,
        )
        self.assertNotEqual(
            bound.patch.idempotency_key,
            bind_proposal(
                snapshot,
                proposal,
                run_id="different-run",
            ).patch.idempotency_key,
        )

        unknown = ProposalIntent(
            operations=proposal.operations,
            reasons=(
                OperationReason(
                    "set-duration",
                    (f"issue-{'f' * 32}",),
                    "set_activity_duration",
                    "Cites an issue outside the snapshot.",
                ),
            ),
        )
        with self.assertRaises(RepairContractError) as caught:
            bind_proposal(snapshot, unknown, run_id="repair-run")
        self.assertEqual(caught.exception.code, "UNKNOWN_ISSUE_ID")

    def test_bind_validates_option_and_stamps_trusted_audit_identity(self) -> None:
        snapshot = _snapshot_with_issue()
        issue_id = snapshot.issues[0].issue_id
        proposal = ProposalIntent(
            operations=(
                AddConstraint(
                    "derived",
                    "constraint-ai",
                    {
                        "kind": "must_include",
                        "strength": "soft",
                        "subject_ids": ["activity-1"],
                        "origin": "user",
                    },
                ),
            ),
            reasons=(
                OperationReason(
                    "derived",
                    (issue_id,),
                    "set_activity_duration",
                    "Deliberately mismatched; controller must checkpoint it.",
                ),
            ),
        )
        bound = bind_proposal(snapshot, proposal, run_id="repair-run")
        operation = bound.patch.operations[0]
        self.assertEqual(operation.op_id, "ai-op-001")
        self.assertEqual(bound.reasons[0].op_id, "ai-op-001")
        self.assertEqual(operation.fields["origin"], "ai")
        self.assertFalse(
            operation_matches_option(
                bound.reasons[0].option_key,
                operation,
            )
        )

        invalid = ProposalIntent(
            operations=proposal.operations,
            reasons=(
                OperationReason(
                    "derived",
                    (issue_id,),
                    "move_activity",
                    "This option is not offered for missing duration.",
                ),
            ),
        )
        with self.assertRaises(RepairContractError) as caught:
            bind_proposal(snapshot, invalid, run_id="repair-run")
        self.assertEqual(caught.exception.code, "INVALID_REPAIR_OPTION")

        mixed_unknown = _snapshot_with_issue(
            _issue(
                fixes=(
                    "set_activity_duration",
                    "future_unreviewed_fix",
                )
            )
        )
        blocked_issue = mixed_unknown.issues[0]
        self.assertFalse(blocked_issue.auto_repairable)
        blocked = ProposalIntent(
            operations=(
                UpdateActivity(
                    "duration",
                    "activity-1",
                    {"duration_min": 45},
                ),
            ),
            reasons=(
                OperationReason(
                    "duration",
                    (blocked_issue.issue_id,),
                    "set_activity_duration",
                    "A known option cannot bypass an unknown sibling fix.",
                ),
            ),
        )
        with self.assertRaises(RepairContractError) as caught:
            bind_proposal(mixed_unknown, blocked, run_id="repair-run")
        self.assertEqual(caught.exception.code, "INVALID_REPAIR_OPTION")


class Phase2RepairDecoderTests(unittest.TestCase):
    def _decode(self, payload: dict[str, Any]) -> ProposalIntent:
        return decode_proposal_intent(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        )

    def test_decoder_accepts_all_eight_semantic_operation_types(self) -> None:
        operations = [
            {
                "op": "add_activity",
                "op_id": "op-add-activity",
                "activity_id": "activity-new",
                "day_id": "day-1",
                "fields": {
                    "title": "New activity",
                    "location_id": "location-new",
                },
                "position": "before",
                "anchor_activity_id": "activity-1",
            },
            {
                "op": "update_activity",
                "op_id": "op-update-activity",
                "activity_id": "activity-1",
                "fields": {"duration_min": 45},
            },
            {
                "op": "place_activity",
                "op_id": "op-place-activity",
                "activity_id": "activity-1",
                "day_id": "day-2",
                "position": "after",
                "anchor_activity_id": "activity-2",
                "scheduled_start": None,
            },
            {
                "op": "remove_activity",
                "op_id": "op-remove-activity",
                "activity_id": "activity-old",
            },
            {
                "op": "update_day",
                "op_id": "op-update-day",
                "day_id": "day-1",
                "fields": {"available_end": "21:00"},
            },
            {
                "op": "add_constraint",
                "op_id": "op-add-constraint",
                "constraint_id": "constraint-new",
                "fields": {
                    "kind": "must_include",
                    "strength": "soft",
                    "subject_ids": ["activity-1"],
                },
            },
            {
                "op": "update_constraint",
                "op_id": "op-update-constraint",
                "constraint_id": "constraint-1",
                "fields": {"strength": "hard"},
            },
            {
                "op": "remove_constraint",
                "op_id": "op-remove-constraint",
                "constraint_id": "constraint-old",
            },
        ]
        decoded = self._decode(_proposal_payload(operations))

        self.assertEqual(
            tuple(type(operation) for operation in decoded.operations),
            (
                AddActivity,
                UpdateActivity,
                PlaceActivity,
                RemoveActivity,
                UpdateDay,
                AddConstraint,
                UpdateConstraint,
                RemoveConstraint,
            ),
        )
        self.assertEqual(decoded.reasons[0].reason, "A" * 300)

    def test_decoder_rejects_duplicate_json_keys(self) -> None:
        duplicate = (
            '{"proposal_version":"repair-proposal/v1",'
            '"proposal_version":"repair-proposal/v1",'
            '"operations":[],"reasons":[]}'
        )
        with self.assertRaises(RepairContractError) as caught:
            decode_proposal_intent(duplicate)
        self.assertEqual(caught.exception.code, "DUPLICATE_JSON_KEY")

    def test_decoder_rejects_model_authored_trusted_fields(self) -> None:
        base = _proposal_payload(
            [
                {
                    "op": "update_activity",
                    "op_id": "op-1",
                    "activity_id": "activity-1",
                    "fields": {"duration_min": 45},
                }
            ]
        )
        for field, value in (
            ("approval", {"approved": True}),
            ("trip_id", "forged-trip"),
            ("base_revision", "0" * 64),
            ("idempotency_key", "model-key"),
            ("status", "feasible"),
        ):
            with self.subTest(field=field):
                payload = deepcopy(base)
                payload[field] = value
                with self.assertRaises(RepairContractError) as caught:
                    self._decode(payload)
                self.assertEqual(
                    caught.exception.code,
                    "FORBIDDEN_PROPOSAL_FIELD",
                )

    def test_decoder_rejects_unknown_operation_and_missing_reason(self) -> None:
        unknown = _proposal_payload(
            [{"op": "delete_trip", "op_id": "op-unknown"}]
        )
        with self.assertRaises(RepairContractError) as caught:
            self._decode(unknown)
        self.assertEqual(caught.exception.code, "UNKNOWN_OPERATION")

        missing_reason = _proposal_payload(
            [
                {
                    "op": "remove_activity",
                    "op_id": "op-1",
                    "activity_id": "activity-1",
                }
            ],
            reasons=[],
        )
        with self.assertRaises(RepairContractError) as caught:
            self._decode(missing_reason)
        self.assertEqual(caught.exception.code, "OPERATION_REASON_MISMATCH")

    def test_decoder_rejects_authority_fields_and_normalizes_bad_reasons(self) -> None:
        forged_origin = _proposal_payload(
            [
                {
                    "op": "add_constraint",
                    "op_id": "op-1",
                    "constraint_id": "constraint-1",
                    "fields": {
                        "kind": "must_include",
                        "subject_ids": ["activity-1"],
                        "origin": "user",
                    },
                }
            ]
        )
        with self.assertRaises(RepairContractError) as caught:
            self._decode(forged_origin)
        self.assertEqual(caught.exception.code, "FORBIDDEN_PROPOSAL_FIELD")

        bad_reason = _proposal_payload(
            [
                {
                    "op": "update_activity",
                    "op_id": "op-1",
                    "activity_id": "activity-1",
                    "fields": {"duration_min": 45},
                }
            ]
        )
        bad_reason["reasons"][0]["issue_ids"] = []
        with self.assertRaises(RepairContractError) as caught:
            self._decode(bad_reason)
        self.assertEqual(caught.exception.code, "MALFORMED_PROPOSAL")

    def test_decoder_rejects_more_than_store_operation_limit(self) -> None:
        operations = [
            {
                "op": "remove_activity",
                "op_id": f"op-{index}",
                "activity_id": f"activity-{index}",
            }
            for index in range(129)
        ]
        with self.assertRaises(RepairContractError) as caught:
            self._decode(_proposal_payload(operations))
        self.assertEqual(caught.exception.code, "PROPOSAL_TOO_COMPLEX")

    def test_decoder_rejects_oversized_payload_before_json_parsing(self) -> None:
        oversized = b"{" + (b" " * (1024 * 1024))
        with self.assertRaises(RepairContractError) as caught:
            decode_proposal_intent(oversized)
        self.assertEqual(caught.exception.code, "PROPOSAL_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
