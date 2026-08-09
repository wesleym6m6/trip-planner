"""Offline adversarial tests for Phase 1 semantic plan mutations."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Callable

from trip_planner.codec import compute_revision, validate_plan
from trip_planner.migrations import preview_legacy_migration
from trip_planner.mutations import (
    AdoptMigratedBaseline,
    AddActivity,
    AddConstraint,
    ApprovalGrant,
    MigratedActivityClassification,
    MigratedActivityClassificationKind,
    PlaceActivity,
    Placement,
    PlanPatch,
    RemoveActivity,
    RemoveConstraint,
    UpdateActivity,
    UpdateConstraint,
    UpdateDay,
    apply_patch_to_plan,
    patch_digest,
    patch_to_dict,
)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _disk_snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _days(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return plan["state"]["itinerary"]["days"]


def _day(plan: dict[str, Any], day_id: str) -> dict[str, Any]:
    return next(day for day in _days(plan) if day["day_id"] == day_id)


def _activities(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [activity for day in _days(plan) for activity in day["places"]]


def _activity(plan: dict[str, Any], activity_id: str) -> dict[str, Any]:
    return next(
        activity
        for activity in _activities(plan)
        if activity["activity_id"] == activity_id
    )


def _activity_ids(plan: dict[str, Any]) -> tuple[str, ...]:
    return tuple(activity["activity_id"] for activity in _activities(plan))


class Phase1MutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "sample-trip" / "data"
        self.data_dir.mkdir(parents=True)

        _write_json(
            self.data_dir / "trip.json",
            {
                "title": "Mutation fixture",
                "subtitle": "offline",
                "date_range": "2026-10-01 ~ 2026-10-02",
                "cities": ["Fixture City"],
                "slug": "mutation-fixture",
                "timezone": "Asia/Taipei",
            },
        )
        _write_json(
            self.data_dir / "itinerary.json",
            {
                "available_modes": ["walking"],
                "days": [
                    {
                        "day": 1,
                        "date": "2026-10-01",
                        "title": "First day",
                        "available_start": "08:00",
                        "available_end": "20:00",
                        "places": [
                            {
                                "title": "Alpha",
                                "location_id": "location-alpha",
                                "time": "09:00",
                                "duration_min": 45,
                                "note": "alpha original",
                            },
                            {
                                "title": "Beta",
                                "location_id": "location-beta",
                                "time": "11:00",
                                "duration_min": 60,
                                "note": "beta original",
                            },
                        ],
                        "travel": [
                            {
                                "from": 0,
                                "to": 1,
                                "modes": {
                                    "walking": {
                                        "duration_min": 12,
                                        "evidence_state": "unverified",
                                    }
                                },
                                "recommended_mode": "walking",
                            }
                        ],
                    },
                    {
                        "day": 2,
                        "date": "2026-10-02",
                        "title": "Second day",
                        "available_start": "08:00",
                        "available_end": "20:00",
                        "places": [
                            {
                                "title": "Gamma",
                                "location_id": "location-gamma",
                                "time": "10:00",
                                "duration_min": 30,
                                "note": "gamma original",
                            }
                        ],
                        "travel": [
                            {
                                "from": 0,
                                "to": 0,
                                "modes": {
                                    "walking": {
                                        "duration_min": 0,
                                        "evidence_state": "unverified",
                                    }
                                },
                                "recommended_mode": "walking",
                            }
                        ],
                    },
                ],
            },
        )

        self.preview = preview_legacy_migration(self.data_dir)
        migrated = self.preview.mutable_candidate_plan()
        self.day_1 = _days(migrated)[0]["day_id"]
        self.day_2 = _days(migrated)[1]["day_id"]
        by_title = {
            activity["title"]: activity["activity_id"]
            for activity in _activities(migrated)
        }
        self.alpha = by_title["Alpha"]
        self.beta = by_title["Beta"]
        self.gamma = by_title["Gamma"]
        self.files_before = _disk_snapshot(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seal(self, plan: dict[str, Any]) -> dict[str, Any]:
        plan["revision"] = compute_revision(plan)
        validate_plan(plan)
        return plan

    def _ordinary_plan(self) -> dict[str, Any]:
        plan = self.preview.mutable_candidate_plan()
        migration = plan["state"]["trip"]["_trip_planner"]["migration"]
        migration["protected_activity_ids"] = []
        for activity in _activities(plan):
            activity["decision_state"] = "selected"
            activity["flexibility"] = "movable"
            activity["evidence_state"] = "unverified"
        plan["receipts"] = {
            "existing": {
                "kind": "patch",
                "status": "applied",
                "request_digest": f"sha256:{'1' * 64}",
                "transaction_id": f"tx-{'2' * 32}",
                "base_revision": "3" * 64,
                "applied_revision": "4" * 64,
                "applied_generation": 1,
                "result_revision": "unchanged",
            }
        }
        return self._seal(plan)

    def _protected_plan(self, decision_state: str) -> dict[str, Any]:
        plan = self._ordinary_plan()
        alpha = _activity(plan, self.alpha)
        alpha["decision_state"] = decision_state
        alpha["flexibility"] = "fixed_time"
        alpha["time"] = "09:00"
        return self._seal(plan)

    def _migrated_unclassified_plan(self) -> dict[str, Any]:
        plan = self.preview.mutable_candidate_plan()
        self.assertNotIn("decision_state", _activity(plan, self.alpha))
        self.assertNotIn("flexibility", _activity(plan, self.alpha))
        return plan

    def _patch(
        self,
        plan: dict[str, Any],
        *operations: Any,
        key: str = "mutation-key",
    ) -> PlanPatch:
        return PlanPatch(
            trip_id=plan["trip_id"],
            base_revision=plan["revision"],
            idempotency_key=key,
            operations=tuple(operations),
            intent="offline mutation fixture",
        )

    def _baseline_adoption(
        self,
        plan: dict[str, Any],
        kinds: dict[str, MigratedActivityClassificationKind],
        *,
        key: str = "baseline-adoption",
    ) -> PlanPatch:
        migration = plan["state"]["trip"]["_trip_planner"]["migration"]
        return self._patch(
            plan,
            AdoptMigratedBaseline(
                op_id="adopt-migrated-baseline",
                source_revision=migration["source_revision"],
                classifications=tuple(
                    MigratedActivityClassification(activity_id, kind)
                    for activity_id, kind in kinds.items()
                ),
            ),
            key=key,
        )

    def _grant(self, scope: str) -> ApprovalGrant:
        return ApprovalGrant(
            approval_id="human-grant",
            scope_digest=scope,
            approved_by="fixture-human",
            approved_at="2026-07-27T00:00:00Z",
        )

    def assert_problem(self, draft: Any, code: str) -> None:
        self.assertIn(code, {problem.code for problem in draft.problems})

    def assert_files_unchanged(self) -> None:
        self.assertEqual(self.files_before, _disk_snapshot(self.root))
        self.assertFalse((self.data_dir / "plan.json").exists())

    def test_typed_add_update_place_remove_patch_is_one_draft(self) -> None:
        plan = self._ordinary_plan()
        original = deepcopy(plan)
        patch = self._patch(
            plan,
            AddActivity(
                "add-delta",
                "activity-delta",
                self.day_1,
                {
                    "title": "Delta",
                    "location_id": "location-delta",
                    "decision_state": "selected",
                    "flexibility": "movable",
                    "evidence_state": "unverified",
                },
                Placement.AFTER,
                self.alpha,
            ),
            UpdateActivity("rename-beta", self.beta, {"title": "Beta renamed"}),
            PlaceActivity(
                "move-alpha",
                self.alpha,
                self.day_2,
                Placement.AFTER,
                self.gamma,
                scheduled_start="15:00",
            ),
            RemoveActivity("remove-beta", self.beta),
        )

        draft = apply_patch_to_plan(plan, patch)

        self.assertTrue(draft.can_apply, draft.problems)
        candidate = draft.to_plan_dict()
        self.assertEqual(
            ("activity-delta",),
            tuple(item["activity_id"] for item in _day(candidate, self.day_1)["places"]),
        )
        self.assertEqual(
            (self.gamma, self.alpha),
            tuple(item["activity_id"] for item in _day(candidate, self.day_2)["places"]),
        )
        self.assertEqual("15:00", _activity(candidate, self.alpha)["time"])
        self.assertEqual(original, plan)
        self.assert_files_unchanged()

        with self.assertRaises(FrozenInstanceError):
            patch.intent = "mutated"  # type: ignore[misc]

    def test_failed_multi_operation_patch_cannot_be_applied(self) -> None:
        plan = self._ordinary_plan()
        original = deepcopy(plan)
        patch = self._patch(
            plan,
            AddActivity(
                "add-first",
                "activity-new",
                self.day_1,
                {"title": "New", "location_id": "location-new"},
            ),
            RemoveActivity("bad-remove", "activity-does-not-exist"),
        )

        draft = apply_patch_to_plan(plan, patch)

        self.assertFalse(draft.can_apply)
        self.assert_problem(draft, "UNKNOWN_ENTITY")
        self.assertEqual(original, draft.to_plan_dict())
        self.assertEqual((), draft.changes)
        self.assertEqual((), draft.affected_day_ids)
        self.assertEqual((), draft.invalidated_day_ids)
        self.assertIsNone(draft.required_approval_scope)
        self.assertEqual(original, plan)
        self.assert_files_unchanged()

    def test_stable_ids_survive_rename_reorder_and_cross_day_move(self) -> None:
        plan = self._ordinary_plan()
        original_ids = set(_activity_ids(plan))
        patch = self._patch(
            plan,
            UpdateActivity(
                "rename-alpha",
                self.alpha,
                {"title": "Alpha has a completely new name"},
            ),
            PlaceActivity(
                "cross-day",
                self.alpha,
                self.day_2,
                Placement.BEFORE,
                self.gamma,
            ),
        )

        draft = apply_patch_to_plan(plan, patch)

        self.assertTrue(draft.can_apply, draft.problems)
        candidate = draft.to_plan_dict()
        self.assertEqual(original_ids, set(_activity_ids(candidate)))
        self.assertEqual(
            self.alpha,
            _day(candidate, self.day_2)["places"][0]["activity_id"],
        )
        self.assertEqual(
            "Alpha has a completely new name",
            _activity(candidate, self.alpha)["title"],
        )

    def test_unknown_targets_are_structured_problems(self) -> None:
        factories: tuple[Callable[[], Any], ...] = (
            lambda: AddActivity(
                "add", "new-id", "unknown-day", {"location_id": "location-new"}
            ),
            lambda: UpdateActivity("update", "unknown-activity", {"note": "x"}),
            lambda: PlaceActivity(
                "place", "unknown-activity", self.day_1, Placement.END
            ),
            lambda: RemoveActivity("remove", "unknown-activity"),
            lambda: UpdateDay("day", "unknown-day", {"title": "x"}),
            lambda: UpdateConstraint("constraint", "unknown-constraint", {"origin": "x"}),
            lambda: RemoveConstraint("remove-constraint", "unknown-constraint"),
        )
        for factory in factories:
            with self.subTest(operation=factory().__class__.__name__):
                plan = self._ordinary_plan()
                draft = apply_patch_to_plan(plan, self._patch(plan, factory()))
                self.assertFalse(draft.can_apply)
                self.assert_problem(draft, "UNKNOWN_ENTITY")

    def test_duplicate_operation_and_entity_ids_are_rejected(self) -> None:
        plan = self._ordinary_plan()
        duplicate_ops = self._patch(
            plan,
            UpdateActivity("same-op", self.alpha, {"note": "one"}),
            UpdateActivity("same-op", self.beta, {"note": "two"}),
        )
        draft = apply_patch_to_plan(plan, duplicate_ops)
        self.assert_problem(draft, "DUPLICATE_OPERATION_ID")
        self.assertEqual(plan, draft.to_plan_dict())

        duplicate_entity = self._patch(
            plan,
            AddActivity(
                "duplicate-activity",
                self.alpha,
                self.day_1,
                {"title": "Duplicate", "location_id": "different-location"},
            ),
        )
        draft = apply_patch_to_plan(plan, duplicate_entity)
        self.assert_problem(draft, "DUPLICATE_ENTITY_ID")

    def test_identity_and_unknown_fields_are_not_mutable(self) -> None:
        plan = self._ordinary_plan()
        for field in ("activity_id", "id", "day_id", "order"):
            with self.subTest(identity_field=field):
                patch = self._patch(
                    plan,
                    UpdateActivity("identity", self.alpha, {field: "replacement"}),
                )
                draft = apply_patch_to_plan(plan, patch)
                self.assert_problem(draft, "IDENTITY_FIELD_FORBIDDEN")
        draft = apply_patch_to_plan(
            plan,
            self._patch(
                plan,
                UpdateActivity(
                    "path-like",
                    self.alpha,
                    {"../../itinerary.days[0]": "not allowed"},
                ),
            ),
        )
        self.assert_problem(draft, "FIELD_NOT_MUTABLE")

    def test_evidence_state_is_authority_owned_and_fact_edits_downgrade(
        self,
    ) -> None:
        plan = self._ordinary_plan()
        for evidence_state in (
            "verified",
            "unverified",
            "stale",
            "conflicted",
        ):
            with self.subTest(existing=evidence_state):
                draft = apply_patch_to_plan(
                    plan,
                    self._patch(
                        plan,
                        UpdateActivity(
                            f"direct-{evidence_state}",
                            self.alpha,
                            {"evidence_state": evidence_state},
                        ),
                    ),
                )
                self.assert_problem(
                    draft, "EVIDENCE_STATE_WRITE_FORBIDDEN"
                )

        for evidence_state in ("verified", "stale", "conflicted"):
            with self.subTest(new=evidence_state):
                draft = apply_patch_to_plan(
                    plan,
                    self._patch(
                        plan,
                        AddActivity(
                            f"add-{evidence_state}",
                            f"activity-{evidence_state}",
                            self.day_1,
                            {
                                "title": "Untrusted",
                                "location_id": (
                                    f"location-{evidence_state}"
                                ),
                                "evidence_state": evidence_state,
                            },
                        ),
                    ),
                )
                self.assert_problem(
                    draft, "EVIDENCE_STATE_WRITE_FORBIDDEN"
                )

        allowed_new = apply_patch_to_plan(
            plan,
            self._patch(
                plan,
                AddActivity(
                    "add-unverified",
                    "activity-unverified",
                    self.day_1,
                    {
                        "title": "Unverified",
                        "location_id": "location-unverified",
                        "evidence_state": "unverified",
                    },
                ),
            ),
        )
        self.assertTrue(allowed_new.can_apply, allowed_new.problems)

        verified = self._ordinary_plan()
        verified_alpha = _activity(verified, self.alpha)
        verified_alpha["evidence_state"] = "verified"
        verified_alpha["duration_min"] = 120
        self._seal(verified)

        readded_fields = {
            key: deepcopy(value)
            for key, value in _activity(verified, self.alpha).items()
            if key not in {"activity_id", "id", "evidence_state"}
        }
        indirect_laundering = apply_patch_to_plan(
            verified,
            self._patch(
                verified,
                RemoveActivity("remove-verified", self.alpha),
                AddActivity(
                    "readd-without-evidence",
                    self.alpha,
                    self.day_1,
                    readded_fields,
                    Placement.START,
                ),
                key="indirect-evidence-laundering",
            ),
        )
        self.assert_problem(
            indirect_laundering, "EVIDENCE_STATE_WRITE_FORBIDDEN"
        )

        evidence_fact_cases = (
            ("duration_min", 10),
            ("location_id", "location-alpha-revised"),
            ("place_id", "place-alpha"),
            (
                "allowed_windows",
                [{"start": "08:30", "end": "12:00"}],
            ),
        )
        for field, value in evidence_fact_cases:
            with self.subTest(fact=field):
                draft = apply_patch_to_plan(
                    verified,
                    self._patch(
                        verified,
                        UpdateActivity(
                            f"change-{field}",
                            self.alpha,
                            {field: value},
                        ),
                    ),
                )
                self.assertTrue(draft.can_apply, draft.problems)
                candidate = draft.to_plan_dict()
                self.assertEqual(
                    "unverified",
                    _activity(candidate, self.alpha)["evidence_state"],
                )
                derived = [
                    change
                    for change in draft.changes
                    if change.entity_id == self.alpha
                    and change.field == "evidence_state"
                ]
                self.assertEqual(1, len(derived))
                self.assertEqual("verified", derived[0].before)
                self.assertEqual("unverified", derived[0].after)
                self.assertEqual(
                    "invalidate_evidence", derived[0].kind
                )
                self.assertEqual("derived", derived[0].op_id)

        cosmetic = apply_patch_to_plan(
            verified,
            self._patch(
                verified,
                UpdateActivity(
                    "cosmetic-note",
                    self.alpha,
                    {"note": "copy edit"},
                ),
            ),
        )
        self.assertTrue(cosmetic.can_apply, cosmetic.problems)
        self.assertEqual(
            "verified",
            _activity(
                cosmetic.to_plan_dict(), self.alpha
            )["evidence_state"],
        )

        round_trip = apply_patch_to_plan(
            verified,
            self._patch(
                verified,
                UpdateActivity(
                    "temporary-duration",
                    self.alpha,
                    {"duration_min": 90},
                ),
                UpdateActivity(
                    "restore-duration",
                    self.alpha,
                    {
                        "duration_min": _activity(
                            verified, self.alpha
                        )["duration_min"]
                    },
                ),
            ),
        )
        self.assertTrue(round_trip.can_apply, round_trip.problems)
        self.assertEqual(verified, round_trip.to_plan_dict())
        self.assertNotIn(
            "evidence_state",
            {change.field for change in round_trip.changes},
        )

    def test_constraint_operations_and_dangling_references(self) -> None:
        plan = self._ordinary_plan()
        add_update_remove = self._patch(
            plan,
            AddConstraint(
                "add-constraint",
                "constraint-alpha",
                {
                    "kind": "must_include",
                    "strength": "hard",
                    "subject_ids": [self.alpha],
                },
            ),
            UpdateConstraint(
                "update-constraint",
                "constraint-alpha",
                {"origin": "user-confirmed"},
            ),
            RemoveConstraint("remove-constraint", "constraint-alpha"),
        )
        draft = apply_patch_to_plan(plan, add_update_remove)
        self.assertTrue(draft.can_apply, draft.problems)
        self.assertEqual([], draft.to_plan_dict()["state"]["trip"]["constraints"])

        dangling_add = self._patch(
            plan,
            AddConstraint(
                "dangling-add",
                "constraint-dangling",
                {
                    "kind": "must_include",
                    "strength": "hard",
                    "subject_ids": ["unknown-activity"],
                },
            ),
        )
        self.assert_problem(
            apply_patch_to_plan(plan, dangling_add), "DEPENDENT_REFERENCE"
        )

        constrained = self._ordinary_plan()
        constrained["state"]["trip"]["constraints"] = [
            {
                "constraint_id": "constraint-existing",
                "kind": "must_include",
                "strength": "hard",
                "subject_ids": [self.alpha],
            }
        ]
        self._seal(constrained)
        dangling_remove = self._patch(
            constrained,
            RemoveActivity("remove-subject", self.alpha),
        )
        self.assert_problem(
            apply_patch_to_plan(constrained, dangling_remove),
            "DEPENDENT_REFERENCE",
        )

    def test_original_hard_constraint_changes_require_exact_approval(
        self,
    ) -> None:
        plan = self._ordinary_plan()
        hard_constraint = {
            "constraint_id": "constraint-hard",
            "kind": "must_include",
            "strength": "hard",
            "subject_ids": [self.alpha],
            "params": {},
            "origin": "user",
            "confidence": 1.0,
            "source_text": "Must include Alpha",
        }
        plan["state"]["trip"]["constraints"] = [
            deepcopy(hard_constraint)
        ]
        self._seal(plan)

        cases = (
            ("strength", {"strength": "soft"}),
            ("kind", {"kind": "exactly_once"}),
            ("subject_ids", {"subject_ids": [self.beta]}),
            ("params", {"params": {"n": 1}}),
            ("origin", {"origin": "ai-rewritten"}),
            ("confidence", {"confidence": 0.5}),
            ("source_text", {"source_text": "Rewritten"}),
        )
        for expected_field, fields in cases:
            with self.subTest(field=expected_field):
                patch = self._patch(
                    plan,
                    UpdateConstraint(
                        f"change-{expected_field}",
                        "constraint-hard",
                        fields,
                    ),
                    key=f"hard-{expected_field}",
                )
                preview = apply_patch_to_plan(plan, patch)
                self.assert_problem(preview, "APPROVAL_REQUIRED")
                self.assertIn(
                    expected_field,
                    {
                        change.field
                        for change in preview.protected_changes
                    },
                )
                approved = apply_patch_to_plan(
                    plan,
                    patch,
                    approvals=(
                        self._grant(preview.required_approval_scope),
                    ),
                )
                self.assertTrue(approved.can_apply, approved.problems)

        remove_patch = self._patch(
            plan,
            RemoveConstraint("remove-hard", "constraint-hard"),
            key="remove-hard",
        )
        removed = apply_patch_to_plan(plan, remove_patch)
        self.assert_problem(removed, "APPROVAL_REQUIRED")
        self.assertIn(
            "$entity",
            {change.field for change in removed.protected_changes},
        )

        downgrade_delete = self._patch(
            plan,
            UpdateConstraint(
                "downgrade-hard",
                "constraint-hard",
                {"strength": "soft"},
            ),
            RemoveConstraint("delete-softened", "constraint-hard"),
            key="downgrade-delete-hard",
        )
        bypass = apply_patch_to_plan(plan, downgrade_delete)
        self.assert_problem(bypass, "APPROVAL_REQUIRED")
        self.assertIn(
            "$entity",
            {change.field for change in bypass.protected_changes},
        )

        round_trip = self._patch(
            plan,
            UpdateConstraint(
                "temporary-origin",
                "constraint-hard",
                {"origin": "temporary"},
            ),
            UpdateConstraint(
                "restore-origin",
                "constraint-hard",
                {"origin": "user"},
            ),
            key="hard-net-noop",
        )
        no_op = apply_patch_to_plan(plan, round_trip)
        self.assertTrue(no_op.can_apply, no_op.problems)
        self.assertEqual((), no_op.protected_changes)
        self.assertEqual(plan, no_op.to_plan_dict())

        soft = deepcopy(plan)
        soft["state"]["trip"]["constraints"][0]["strength"] = "soft"
        self._seal(soft)
        soft_remove = apply_patch_to_plan(
            soft,
            self._patch(
                soft,
                RemoveConstraint("remove-soft", "constraint-hard"),
                key="remove-soft",
            ),
        )
        self.assertTrue(soft_remove.can_apply, soft_remove.problems)
        self.assertIsNone(soft_remove.required_approval_scope)

    def test_non_target_entities_and_caller_snapshot_are_unchanged(self) -> None:
        plan = self._ordinary_plan()
        original = deepcopy(plan)
        beta_before = deepcopy(_activity(plan, self.beta))
        day_2_before = deepcopy(_day(plan, self.day_2))
        trip_before = deepcopy(plan["state"]["trip"])
        receipts_before = deepcopy(plan["receipts"])

        draft = apply_patch_to_plan(
            plan,
            self._patch(
                plan,
                UpdateActivity("note-alpha", self.alpha, {"note": "new note"}),
            ),
        )

        self.assertTrue(draft.can_apply, draft.problems)
        candidate = draft.to_plan_dict()
        self.assertEqual(beta_before, _activity(candidate, self.beta))
        self.assertEqual(day_2_before, _day(candidate, self.day_2))
        self.assertEqual(trip_before, candidate["state"]["trip"])
        self.assertEqual(receipts_before, candidate["receipts"])
        self.assertEqual(original, plan)

    def test_topology_time_location_and_day_changes_invalidate_travel(self) -> None:
        cases: tuple[tuple[str, Callable[[], Any]], ...] = (
            (
                "topology",
                lambda: PlaceActivity(
                    "reorder",
                    self.alpha,
                    self.day_1,
                    Placement.AFTER,
                    self.beta,
                ),
            ),
            (
                "time",
                lambda: UpdateActivity(
                    "change-time", self.alpha, {"time": "09:30"}
                ),
            ),
            (
                "location",
                lambda: UpdateActivity(
                    "change-location",
                    self.alpha,
                    {"location_id": "location-alpha-new"},
                ),
            ),
            (
                "day",
                lambda: UpdateDay(
                    "change-day-timezone",
                    self.day_1,
                    {"timezone": "Asia/Tokyo"},
                ),
            ),
        )
        for label, factory in cases:
            with self.subTest(change=label):
                plan = self._ordinary_plan()
                edge_before = deepcopy(_day(plan, self.day_1)["travel"][0])
                draft = apply_patch_to_plan(
                    plan, self._patch(plan, factory(), key=f"key-{label}")
                )
                self.assertTrue(draft.can_apply, draft.problems)
                candidate = draft.to_plan_dict()
                self.assertEqual([], _day(candidate, self.day_1)["travel"])
                self.assertIn(self.day_1, draft.invalidated_day_ids)
                self.assertEqual(self.alpha, edge_before["from_activity_id"])
                self.assertEqual(self.beta, edge_before["to_activity_id"])
                self.assertNotIn(edge_before, _day(candidate, self.day_1)["travel"])

    def test_cross_day_move_invalidates_both_days_without_retargeting(self) -> None:
        plan = self._ordinary_plan()
        source_edges = deepcopy(_day(plan, self.day_1)["travel"])
        target_edges = deepcopy(_day(plan, self.day_2)["travel"])
        draft = apply_patch_to_plan(
            plan,
            self._patch(
                plan,
                PlaceActivity(
                    "cross-day",
                    self.alpha,
                    self.day_2,
                    Placement.AFTER,
                    self.gamma,
                ),
            ),
        )

        self.assertTrue(draft.can_apply, draft.problems)
        candidate = draft.to_plan_dict()
        self.assertEqual([], _day(candidate, self.day_1)["travel"])
        self.assertEqual([], _day(candidate, self.day_2)["travel"])
        self.assertEqual((self.day_1, self.day_2), draft.invalidated_day_ids)
        self.assertEqual(self.alpha, source_edges[0]["from_activity_id"])
        self.assertEqual(self.gamma, target_edges[0]["from_activity_id"])

    def test_cosmetic_activity_and_day_edits_keep_travel(self) -> None:
        plan = self._ordinary_plan()
        travel_before = deepcopy(_day(plan, self.day_1)["travel"])
        draft = apply_patch_to_plan(
            plan,
            self._patch(
                plan,
                UpdateActivity(
                    "cosmetic-activity",
                    self.alpha,
                    {"title": "Alpha display rename", "note": "copy edit"},
                ),
                UpdateDay(
                    "cosmetic-day",
                    self.day_1,
                    {"title": "A renamed day", "subtitle": "copy edit"},
                ),
            ),
        )

        self.assertTrue(draft.can_apply, draft.problems)
        self.assertEqual(
            travel_before,
            _day(draft.to_plan_dict(), self.day_1)["travel"],
        )
        self.assertNotIn(self.day_1, draft.invalidated_day_ids)

    def test_fixed_and_booked_destructive_changes_require_exact_grant(self) -> None:
        for decision in ("fixed", "booked"):
            with self.subTest(decision=decision):
                plan = self._protected_plan(decision)
                patch = self._patch(
                    plan,
                    PlaceActivity(
                        "move-protected",
                        self.alpha,
                        self.day_2,
                        Placement.END,
                        scheduled_start="14:00",
                    ),
                    key=f"protected-{decision}",
                )
                preview = apply_patch_to_plan(plan, patch)
                self.assertFalse(preview.can_apply)
                self.assert_problem(preview, "APPROVAL_REQUIRED")
                self.assertIsNotNone(preview.required_approval_scope)

                approved = apply_patch_to_plan(
                    plan,
                    patch,
                    approvals=(self._grant(preview.required_approval_scope),),
                )
                self.assertTrue(approved.can_apply, approved.problems)
                self.assertTrue(approved.approval_granted)

    def test_migrated_unclassified_destructive_change_requires_grant(self) -> None:
        plan = self._migrated_unclassified_plan()
        self.assertIn(
            self.alpha,
            plan["state"]["trip"]["_trip_planner"]["migration"][
                "protected_activity_ids"
            ],
        )
        patch = self._patch(
            plan,
            RemoveActivity("remove-unclassified", self.alpha),
        )

        preview = apply_patch_to_plan(plan, patch)

        self.assert_problem(preview, "APPROVAL_REQUIRED")
        approved = apply_patch_to_plan(
            plan,
            patch,
            approvals=(self._grant(preview.required_approval_scope),),
        )
        self.assertTrue(approved.can_apply, approved.problems)
        self.assertNotIn(
            self.alpha,
            approved.to_plan_dict()["state"]["trip"]["_trip_planner"][
                "migration"
            ]["protected_activity_ids"],
        )

    def test_migrated_baseline_adoption_is_complete_exact_and_protected(
        self,
    ) -> None:
        plan = self._migrated_unclassified_plan()
        before = deepcopy(plan)
        patch = self._baseline_adoption(
            plan,
            {
                self.alpha: MigratedActivityClassificationKind.MOVABLE,
                self.beta: MigratedActivityClassificationKind.FIXED_DAY,
                self.gamma: MigratedActivityClassificationKind.BOOKED,
            },
        )

        preview = apply_patch_to_plan(plan, patch)

        self.assert_problem(preview, "APPROVAL_REQUIRED")
        self.assertIsNotNone(preview.required_approval_scope)
        self.assertEqual(
            {self.alpha, self.beta, self.gamma},
            {
                change.entity_id
                for change in preview.protected_changes
                if change.field == "migration.protection"
            },
        )
        approved = apply_patch_to_plan(
            plan,
            patch,
            approvals=(self._grant(preview.required_approval_scope),),
        )
        self.assertTrue(approved.can_apply, approved.problems)
        adopted = approved.to_plan_dict()
        migration = adopted["state"]["trip"]["_trip_planner"]["migration"]
        before_migration = before["state"]["trip"]["_trip_planner"][
            "migration"
        ]
        self.assertEqual([], migration["protected_activity_ids"])
        self.assertEqual(
            before_migration["source_schema"],
            migration["source_schema"],
        )
        self.assertEqual(
            before_migration["source_revision"],
            migration["source_revision"],
        )
        self.assertEqual(
            before_migration["ignored_travel_edges"],
            migration["ignored_travel_edges"],
        )
        expected = {
            self.alpha: ("selected", "movable"),
            self.beta: ("fixed", "fixed_day"),
            self.gamma: ("booked", "fixed_time"),
        }
        for activity_id, fields in expected.items():
            activity = _activity(adopted, activity_id)
            original = _activity(before, activity_id)
            self.assertEqual(fields[0], activity["decision_state"])
            self.assertEqual(fields[1], activity["flexibility"])
            for field in (
                "time",
                "duration_min",
                "location_id",
                "note",
            ):
                self.assertEqual(original.get(field), activity.get(field))
            self.assertEqual(
                original.get("evidence_state"),
                activity.get("evidence_state"),
            )

        movable_change = apply_patch_to_plan(
            adopted,
            self._patch(
                adopted,
                UpdateActivity("move-adopted", self.alpha, {"time": "09:15"}),
                key="move-adopted",
            ),
        )
        self.assertTrue(movable_change.can_apply, movable_change.problems)
        for activity_id in (self.beta, self.gamma):
            protected_change = apply_patch_to_plan(
                adopted,
                self._patch(
                    adopted,
                    UpdateActivity(
                        f"move-{activity_id}",
                        activity_id,
                        {"time": "12:15"},
                    ),
                    key=f"move-{activity_id}",
                ),
            )
            self.assert_problem(protected_change, "APPROVAL_REQUIRED")

    def test_migrated_baseline_adoption_rejects_partial_mixed_and_repeat(
        self,
    ) -> None:
        plan = self._migrated_unclassified_plan()
        with self.assertRaises(ValueError):
            AdoptMigratedBaseline(
                op_id="duplicate-classification",
                source_revision=(
                    plan["state"]["trip"]["_trip_planner"]["migration"][
                        "source_revision"
                    ]
                ),
                classifications=(
                    MigratedActivityClassification(
                        self.alpha,
                        MigratedActivityClassificationKind.MOVABLE,
                    ),
                    MigratedActivityClassification(
                        self.alpha,
                        MigratedActivityClassificationKind.FIXED_DAY,
                    ),
                ),
            )
        partial = self._baseline_adoption(
            plan,
            {self.alpha: MigratedActivityClassificationKind.MOVABLE},
            key="partial-adoption",
        )
        self.assert_problem(
            apply_patch_to_plan(plan, partial),
            "MIGRATED_BASELINE_CLASSIFICATION_MISMATCH",
        )

        mixed = self._patch(
            plan,
            partial.operations[0],
            UpdateActivity("mixed-edit", self.alpha, {"note": "mixed"}),
            key="mixed-adoption",
        )
        self.assert_problem(
            apply_patch_to_plan(plan, mixed),
            "BASELINE_ADOPTION_MUST_BE_EXCLUSIVE",
        )

        wrong_source = self._baseline_adoption(
            plan,
            {
                activity_id: MigratedActivityClassificationKind.MOVABLE
                for activity_id in (self.alpha, self.beta, self.gamma)
            },
            key="wrong-source-adoption",
        )
        wrong_operation = wrong_source.operations[0]
        assert isinstance(wrong_operation, AdoptMigratedBaseline)
        wrong_source = PlanPatch(
            trip_id=wrong_source.trip_id,
            base_revision=wrong_source.base_revision,
            idempotency_key=wrong_source.idempotency_key,
            operations=(
                AdoptMigratedBaseline(
                    op_id=wrong_operation.op_id,
                    source_revision="changed-source-revision",
                    classifications=wrong_operation.classifications,
                ),
            ),
        )
        self.assert_problem(
            apply_patch_to_plan(plan, wrong_source),
            "MIGRATED_BASELINE_SOURCE_CHANGED",
        )

        full = self._baseline_adoption(
            plan,
            {
                activity_id: MigratedActivityClassificationKind.MOVABLE
                for activity_id in (self.alpha, self.beta, self.gamma)
            },
            key="full-adoption",
        )
        preview = apply_patch_to_plan(plan, full)
        adopted = apply_patch_to_plan(
            plan,
            full,
            approvals=(self._grant(preview.required_approval_scope),),
        ).to_plan_dict()
        adopted["generation"] += 1
        self._seal(adopted)
        repeat = PlanPatch(
            trip_id=adopted["trip_id"],
            base_revision=adopted["revision"],
            idempotency_key="repeat-adoption",
            operations=full.operations,
        )
        self.assert_problem(
            apply_patch_to_plan(adopted, repeat),
            "MIGRATED_BASELINE_ALREADY_ADOPTED",
        )

    def test_migrated_baseline_fixed_time_requires_existing_time(self) -> None:
        plan = self._migrated_unclassified_plan()
        _activity(plan, self.alpha).pop("time", None)
        self._seal(plan)
        patch = self._baseline_adoption(
            plan,
            {
                self.alpha: MigratedActivityClassificationKind.BOOKED,
                self.beta: MigratedActivityClassificationKind.MOVABLE,
                self.gamma: MigratedActivityClassificationKind.MOVABLE,
            },
            key="missing-fixed-time",
        )
        self.assert_problem(
            apply_patch_to_plan(plan, patch),
            "FIXED_TIME_REQUIRES_SCHEDULED_TIME",
        )

    def test_migrated_baseline_rejects_foreign_schema_and_reactivation(
        self,
    ) -> None:
        foreign = self._migrated_unclassified_plan()
        foreign["state"]["trip"]["_trip_planner"]["migration"][
            "source_schema"
        ] = "future-v2"
        self._seal(foreign)
        foreign_patch = self._baseline_adoption(
            foreign,
            {
                activity_id: MigratedActivityClassificationKind.MOVABLE
                for activity_id in (self.alpha, self.beta, self.gamma)
            },
            key="foreign-schema-adoption",
        )
        self.assert_problem(
            apply_patch_to_plan(foreign, foreign_patch),
            "MIGRATED_BASELINE_SOURCE_SCHEMA_UNSUPPORTED",
        )

        for decision_state in ("candidate", "cancelled", "excluded"):
            with self.subTest(decision_state=decision_state):
                inactive = self._migrated_unclassified_plan()
                activity = _activity(inactive, self.alpha)
                activity["decision_state"] = decision_state
                activity["flexibility"] = "movable"
                self._seal(inactive)
                inactive_patch = self._baseline_adoption(
                    inactive,
                    {
                        activity_id: MigratedActivityClassificationKind.MOVABLE
                        for activity_id in (
                            self.alpha,
                            self.beta,
                            self.gamma,
                        )
                    },
                    key=f"reactivation-{decision_state}",
                )
                self.assert_problem(
                    apply_patch_to_plan(inactive, inactive_patch),
                    "MIGRATED_BASELINE_REACTIVATION_FORBIDDEN",
                )

    def test_cosmetic_edit_of_protected_activity_needs_no_grant(self) -> None:
        plans = (
            self._protected_plan("booked"),
            self._migrated_unclassified_plan(),
        )
        for index, plan in enumerate(plans):
            with self.subTest(protection=index):
                draft = apply_patch_to_plan(
                    plan,
                    self._patch(
                        plan,
                        UpdateActivity(
                            "cosmetic",
                            self.alpha,
                            {"title": "Corrected display title", "note": "copy edit"},
                        ),
                        key=f"cosmetic-{index}",
                    ),
                )
                self.assertTrue(draft.can_apply, draft.problems)
                self.assertIsNone(draft.required_approval_scope)

    def test_downgrade_then_delete_cannot_bypass_original_snapshot_policy(self) -> None:
        plan = self._protected_plan("booked")
        patch = self._patch(
            plan,
            UpdateActivity(
                "downgrade",
                self.alpha,
                {"decision_state": "selected", "flexibility": "movable"},
            ),
            RemoveActivity("delete", self.alpha),
            key="downgrade-delete",
        )

        draft = apply_patch_to_plan(plan, patch)

        self.assert_problem(draft, "APPROVAL_REQUIRED")
        self.assertIn(
            "$entity",
            {change.field for change in draft.protected_changes},
        )

    def test_protected_position_windows_and_day_context_use_net_approval(self) -> None:
        plan = self._protected_plan("booked")
        alpha_fields = {
            key: deepcopy(value)
            for key, value in _activity(plan, self.alpha).items()
            if key not in {"activity_id", "id"}
        }
        patch = self._patch(
            plan,
            RemoveActivity("remove-booked", self.alpha),
            AddActivity(
                "readd-booked",
                self.alpha,
                self.day_1,
                alpha_fields,
                Placement.END,
            ),
            UpdateActivity(
                "change-windows",
                self.alpha,
                {
                    "allowed_windows": [
                        {"start": "12:00", "end": "18:00"}
                    ]
                },
            ),
            UpdateDay(
                "change-day-context",
                self.day_1,
                {
                    "allowed_modes": ["transit"],
                    "available_end": "22:00",
                    "available_start": "07:00",
                    "date": "2026-10-03",
                    "day": 3,
                    "timezone": "Asia/Tokyo",
                },
            ),
            key="protected-net-context",
        )

        preview = apply_patch_to_plan(plan, patch)

        self.assert_problem(preview, "APPROVAL_REQUIRED")
        self.assertEqual(
            (self.beta, self.alpha),
            tuple(
                activity["activity_id"]
                for activity in _day(
                    preview.to_plan_dict(), self.day_1
                )["places"]
            ),
        )
        protected_fields = {
            change.field for change in preview.protected_changes
        }
        self.assertTrue(
            {
                "position",
                "allowed_windows",
                "day.allowed_modes",
                "day.available_end",
                "day.available_start",
                "day.date",
                "day.day",
                "day.timezone",
            }.issubset(protected_fields),
            protected_fields,
        )

        approved = apply_patch_to_plan(
            plan,
            patch,
            approvals=(self._grant(preview.required_approval_scope),),
        )
        self.assertTrue(approved.can_apply, approved.problems)

    def test_adding_a_predecessor_cannot_silently_shift_booked_activity(self) -> None:
        plan = self._protected_plan("booked")
        patch = self._patch(
            plan,
            AddActivity(
                "insert-before-booking",
                "activity-predecessor",
                self.day_1,
                {
                    "title": "Predecessor",
                    "location_id": "location-predecessor",
                },
                Placement.BEFORE,
                self.alpha,
            ),
            key="protected-indirect-position",
        )

        preview = apply_patch_to_plan(plan, patch)

        self.assert_problem(preview, "APPROVAL_REQUIRED")
        self.assertIn(
            "position",
            {change.field for change in preview.protected_changes},
        )
        self.assertEqual(
            "activity-predecessor",
            _day(preview.to_plan_dict(), self.day_1)["places"][0][
                "activity_id"
            ],
        )

    def test_net_noop_does_not_invalidate_travel_or_migration_metadata(self) -> None:
        plan = self._ordinary_plan()
        original = deepcopy(plan)
        round_trip = self._patch(
            plan,
            UpdateActivity(
                "temporary-location",
                self.alpha,
                {"location_id": "location-temporary"},
            ),
            UpdateActivity(
                "restore-location",
                self.alpha,
                {"location_id": "location-alpha"},
            ),
            key="net-noop-update",
        )
        draft = apply_patch_to_plan(plan, round_trip)
        self.assertTrue(draft.can_apply, draft.problems)
        self.assertEqual(original, draft.to_plan_dict())
        self.assertEqual((), draft.invalidated_day_ids)

        add_remove = self._patch(
            plan,
            AddActivity(
                "temporary-add",
                "activity-temporary",
                self.day_1,
                {
                    "title": "Temporary",
                    "location_id": "location-temporary",
                },
            ),
            RemoveActivity("temporary-remove", "activity-temporary"),
            key="net-noop-add-remove",
        )
        draft = apply_patch_to_plan(plan, add_remove)
        self.assertTrue(draft.can_apply, draft.problems)
        self.assertEqual(original, draft.to_plan_dict())
        self.assertEqual((), draft.invalidated_day_ids)

    def test_patch_request_budgets_reject_amplification_inputs(self) -> None:
        plan = self._ordinary_plan()
        for invalid_id in (
            " leading-space",
            "trailing-space ",
            "control\u0000character",
            "x" * 257,
        ):
            with self.subTest(invalid_id=repr(invalid_id)):
                with self.assertRaises(ValueError):
                    UpdateActivity(invalid_id, self.alpha, {"note": "x"})

        with self.assertRaises(ValueError):
            PlanPatch(
                trip_id=plan["trip_id"],
                base_revision=plan["revision"],
                idempotency_key="intent-too-large",
                operations=(
                    UpdateActivity("bounded", self.alpha, {"note": "x"}),
                ),
                intent="x" * 4097,
            )

        with self.assertRaises(ValueError):
            PlanPatch(
                trip_id=plan["trip_id"],
                base_revision=plan["revision"],
                idempotency_key="too-many-operations",
                operations=tuple(
                    UpdateActivity(
                        f"operation-{index}",
                        self.alpha,
                        {"note": str(index)},
                    )
                    for index in range(129)
                ),
            )

        oversized = self._patch(
            plan,
            UpdateActivity(
                "oversized-payload",
                self.alpha,
                {"note": "x" * (1024 * 1024)},
            ),
            key="oversized-payload",
        )
        draft = apply_patch_to_plan(plan, oversized)
        self.assert_problem(draft, "PATCH_SCHEMA_INVALID")
        self.assertEqual(plan, draft.to_plan_dict())
        self.assertEqual((), draft.changes)

    def test_wrong_scope_revision_or_patch_cannot_approve(self) -> None:
        plan = self._protected_plan("booked")
        patch = self._patch(
            plan,
            UpdateActivity("time", self.alpha, {"time": "10:00"}),
            key="approval-source",
        )
        preview = apply_patch_to_plan(plan, patch)
        grant = self._grant(preview.required_approval_scope)
        self.assertTrue(
            apply_patch_to_plan(plan, patch, approvals=(grant,)).can_apply
        )

        wrong_scope = ApprovalGrant(
            approval_id="wrong",
            scope_digest="sha256:" + "0" * 64,
            approved_by="fixture-human",
            approved_at="2026-07-27T00:00:00Z",
        )
        draft = apply_patch_to_plan(plan, patch, approvals=(wrong_scope,))
        self.assert_problem(draft, "APPROVAL_SCOPE_MISMATCH")

        different_patch = self._patch(
            plan,
            UpdateActivity("time", self.alpha, {"time": "11:00"}),
            key="different-patch",
        )
        draft = apply_patch_to_plan(plan, different_patch, approvals=(grant,))
        self.assert_problem(draft, "APPROVAL_SCOPE_MISMATCH")

        next_revision = deepcopy(plan)
        next_revision["generation"] += 1
        self._seal(next_revision)
        same_change_new_revision = self._patch(
            next_revision,
            UpdateActivity("time", self.alpha, {"time": "10:00"}),
            key="approval-source",
        )
        draft = apply_patch_to_plan(
            next_revision,
            same_change_new_revision,
            approvals=(grant,),
        )
        self.assert_problem(draft, "APPROVAL_SCOPE_MISMATCH")

        stale_patch = PlanPatch(
            trip_id=plan["trip_id"],
            base_revision="not-the-current-revision",
            idempotency_key="stale",
            operations=(
                UpdateActivity("time", self.alpha, {"time": "10:00"}),
            ),
        )
        draft = apply_patch_to_plan(plan, stale_patch, approvals=(grant,))
        self.assert_problem(draft, "STALE_REVISION")
        self.assertFalse(draft.can_apply)

    def test_patch_digest_is_deterministic_and_complete(self) -> None:
        plan = self._ordinary_plan()
        first = self._patch(
            plan,
            UpdateActivity(
                "ordered-fields",
                self.alpha,
                {"note": "same", "priority": 4},
            ),
            key="digest-key",
        )
        second = self._patch(
            plan,
            UpdateActivity(
                "ordered-fields",
                self.alpha,
                {"priority": 4, "note": "same"},
            ),
            key="digest-key",
        )
        changed = self._patch(
            plan,
            UpdateActivity(
                "ordered-fields",
                self.alpha,
                {"priority": 5, "note": "same"},
            ),
            key="digest-key",
        )

        self.assertEqual(patch_to_dict(first), patch_to_dict(second))
        self.assertEqual(patch_digest(first), patch_digest(second))
        self.assertNotEqual(patch_digest(first), patch_digest(changed))

    def test_pure_engine_never_writes_legacy_or_canonical_files(self) -> None:
        plan = self._ordinary_plan()
        original = deepcopy(plan)
        patch = self._patch(
            plan,
            UpdateActivity("pure", self.alpha, {"note": "in memory only"}),
        )

        for _ in range(3):
            draft = apply_patch_to_plan(plan, patch)
            self.assertTrue(draft.can_apply, draft.problems)

        self.assertEqual(original, plan)
        self.assert_files_unchanged()


if __name__ == "__main__":
    unittest.main()
