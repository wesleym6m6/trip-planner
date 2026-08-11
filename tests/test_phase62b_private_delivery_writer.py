from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import trip_planner.private_delivery_writer as writer_module

from trip_planner.codec import compute_revision, encode_plan
from trip_planner.evidence_store import EvidenceStore, EvidenceStoreError
from trip_planner.private_delivery import PrivateDeliveryProfile
from trip_planner.private_delivery_writer import (
    PrivateDeliveryWriteError,
    PrivateDeliveryWriteOutcomeKind,
    PrivateDeliveryWriteResponseKind,
    capture_private_delivery_write_response,
    execute_private_delivery_write_response,
    prepare_private_delivery_write_review,
    reconcile_private_delivery_write_response,
)
from trip_planner.store import TripStore
from tests.test_phase62_private_delivery import EVALUATION_AT, _route_case
from tests.test_phase4_evidence_store import (
    MutableClock,
    authorized_success,
    policy_registry,
    route_key,
    route_observation,
)


PRIVATE_SENTINEL = "SYNTHETIC-PRIVATE-WRITER-SENTINEL"


class _Clock:
    def __init__(self, value: datetime = EVALUATION_AT) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _ReadOnlyEvidenceSource:
    def __init__(
        self,
        trip_id: str,
        snapshot,
        *,
        slug: str,
        trips_root: Path,
        data_dir: Path,
    ) -> None:
        self.trip_id = trip_id
        self.slug = slug
        self.trips_root = trips_root
        self.data_dir = data_dir
        self.snapshot_value = snapshot
        self.read_count = 0
        self.on_read = None

    def read_snapshot(self, *, evaluation_at: datetime):
        self.read_count += 1
        if self.on_read is not None:
            self.on_read(self.read_count)
        if evaluation_at != self.snapshot_value.evaluation_at:
            raise ValueError("synthetic evaluation mismatch")
        return self.snapshot_value


class _Fixture:
    def __init__(self, testcase: unittest.TestCase) -> None:
        self.testcase = testcase
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.trips_root = self.root / "trips"
        self.slug = "synthetic-delivery"
        self.trip_dir = self.trips_root / self.slug
        self.data_dir = self.trip_dir / "data"
        self.private_root = self.trip_dir / "private-delivery"
        for path in (
            self.trips_root,
            self.trip_dir,
            self.data_dir,
            self.private_root,
        ):
            path.mkdir(mode=0o700)
            path.chmod(0o700)
        self.plan, self.snapshot, self.composed, _summary, self.intake = (
            _route_case()
        )
        self.plan_path = self.data_dir / "plan.json"
        self.plan_path.write_bytes(encode_plan(self.plan))
        self.plan_path.chmod(0o600)
        self.store = TripStore(self.trips_root, self.slug)
        self.source = _ReadOnlyEvidenceSource(
            self.plan["trip_id"],
            self.snapshot,
            slug=self.slug,
            trips_root=self.trips_root,
            data_dir=self.data_dir,
        )
        self.clock = _Clock()

    def cleanup(self) -> None:
        self.temp.cleanup()

    def prepare(
        self,
        profile: PrivateDeliveryProfile = PrivateDeliveryProfile.HTML_PREVIEW,
        *,
        target_leaf: str = "generation-a",
        fault_hook=None,
    ):
        kwargs = {}
        if profile is PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE:
            kwargs["lodging_intake"] = self.intake
        return prepare_private_delivery_write_review(
            self.store,
            self.source,
            profile=profile,
            private_root=self.private_root,
            target_leaf=target_leaf,
            clock=self.clock,
            fault_hook=fault_hook,
            **kwargs,
        )


def _error_code(callable_object) -> str:
    with unittest.TestCase().assertRaises(PrivateDeliveryWriteError) as caught:
        callable_object()
    return caught.exception.code


class Phase62BPrivateDeliveryWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _Fixture(self)

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_prepare_reloads_twice_pins_absent_target_and_does_not_write(self) -> None:
        review = self.fixture.prepare()
        self.assertEqual(2, self.fixture.source.read_count)
        target = self.fixture.private_root / "generation-a"
        self.assertFalse(target.exists())
        safe = review.to_safe_dict()
        self.assertTrue(safe["authoritative_source_reloaded"])
        self.assertTrue(safe["target_parent_verified_at_review"])
        self.assertTrue(safe["target_absent_at_review"])
        self.assertFalse(safe["authorization_response_captured"])
        private = review.to_ephemeral_private_review()
        self.assertEqual(str(self.fixture.plan_path), private["source_path"])
        self.assertEqual(str(target), private["target_path"])
        self.assertFalse(private["portable_multi_file_atomicity_claimed"])
        self.assertFalse(
            private["external_phase62a_review_or_response_consumed"]
        )
        self.assertTrue(private["matching_fresh_write_response_required"])
        self.assertTrue(private["one_create_attempt_per_response"])
        self.assertTrue(private["partial_target_may_remain"])
        self.assertFalse(private["cross_process_recovery_authorized"])

    def test_html_preview_create_only_success_has_exact_tree_and_modes(self) -> None:
        stages: list[str] = []
        review = self.fixture.prepare(fault_hook=stages.append)
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        response_safe = response.to_safe_dict()
        self.assertTrue(
            response_safe["accepted_for_prewrite_recheck_at_capture"]
        )
        self.assertTrue(
            response_safe["current_execution_eligibility_not_asserted"]
        )
        outcome = execute_private_delivery_write_response(response)
        self.assertIs(PrivateDeliveryWriteOutcomeKind.CREATED, outcome.kind)
        self.assertTrue(outcome.final_target_published)
        target = self.fixture.private_root / "generation-a"
        self.assertEqual(0o700, stat.S_IMODE(target.stat().st_mode))
        self.assertEqual(
            {"index.html", "manifest.json"},
            {item.name for item in target.iterdir()},
        )
        artifacts = {item.filename: item for item in review.artifacts}
        for name, artifact in artifacts.items():
            path = target / name
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(artifact.payload, path.read_bytes())
        self.assertLess(
            stages.index("after_index_html_fsync"),
            stages.index("after_manifest_create"),
        )
        self.assertLess(
            stages.index("after_manifest_fsync"),
            stages.index("after_parent_directory_fsync"),
        )

    def test_ready_bundle_writes_exact_reviewed_html_ics_and_manifest(self) -> None:
        review = self.fixture.prepare(
            PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE
        )
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_ICS_READY_BUNDLE_CREATE_ONLY_WRITE,
        )
        outcome = execute_private_delivery_write_response(response)
        self.assertIs(PrivateDeliveryWriteOutcomeKind.CREATED, outcome.kind)
        target = self.fixture.private_root / "generation-a"
        self.assertEqual(
            {"index.html", "calendar.ics", "manifest.json"},
            {item.name for item in target.iterdir()},
        )
        manifest = json.loads((target / "manifest.json").read_bytes())
        self.assertEqual("travel_ready", manifest["readiness"]["status"])

    def test_request_changes_cancel_and_cross_profile_never_write(self) -> None:
        for index, kind in enumerate(
            (
                PrivateDeliveryWriteResponseKind.REQUEST_CHANGES,
                PrivateDeliveryWriteResponseKind.CANCEL,
            )
        ):
            leaf = f"generation-{index}"
            review = self.fixture.prepare(target_leaf=leaf)
            response = capture_private_delivery_write_response(review, kind)
            outcome = execute_private_delivery_write_response(response)
            self.assertFalse(outcome.filesystem_mutation_started)
            self.assertFalse((self.fixture.private_root / leaf).exists())

        wrong = self.fixture.prepare(target_leaf="generation-wrong")
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_RESPONSE_PROFILE_MISMATCH",
            _error_code(
                lambda: capture_private_delivery_write_response(
                    wrong,
                    PrivateDeliveryWriteResponseKind.
                    AUTHORIZE_HTML_ICS_READY_BUNDLE_CREATE_ONLY_WRITE,
                )
            ),
        )

    def test_response_capture_and_execute_are_one_shot(self) -> None:
        review = self.fixture.prepare()
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_RESPONSE_ALREADY_CAPTURED",
            _error_code(
                lambda: capture_private_delivery_write_response(
                    review,
                    PrivateDeliveryWriteResponseKind.CANCEL,
                )
            ),
        )
        execute_private_delivery_write_response(response)
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_RESPONSE_ALREADY_EXECUTED",
            _error_code(lambda: execute_private_delivery_write_response(response)),
        )

    def test_source_drift_fails_before_target_creation(self) -> None:
        review = self.fixture.prepare()
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        changed = copy.deepcopy(self.fixture.plan)
        changed["state"]["trip"]["subtitle"] = PRIVATE_SENTINEL
        changed["revision"] = compute_revision(changed)
        self.fixture.plan_path.write_bytes(encode_plan(changed))
        self.fixture.plan_path.chmod(0o600)
        self.assertIn(
            _error_code(lambda: execute_private_delivery_write_response(response)),
            {
                "PRIVATE_DELIVERY_WRITE_SOURCE_STALE",
                "PRIVATE_DELIVERY_WRITE_REVIEW_STALE",
            },
        )
        self.assertFalse((self.fixture.private_root / "generation-a").exists())

    def test_evidence_drift_and_wrong_trip_fail_before_target_creation(self) -> None:
        review = self.fixture.prepare()
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        changed = copy.deepcopy(self.fixture.snapshot)
        object.__setattr__(changed, "store_revision", "f" * 64)
        self.fixture.source.snapshot_value = changed
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_REPROJECTION_REFUSED",
            _error_code(lambda: execute_private_delivery_write_response(response)),
        )
        self.assertFalse((self.fixture.private_root / "generation-a").exists())

        second = _Fixture(self)
        try:
            second.source.trip_id = "wrong-synthetic-trip"
            self.assertEqual(
                "PRIVATE_DELIVERY_WRITE_EVIDENCE_UNAVAILABLE",
                _error_code(lambda: second.prepare()),
            )
        finally:
            second.cleanup()

    def test_target_existing_or_private_root_drift_fails_closed(self) -> None:
        (self.fixture.private_root / "generation-a").mkdir(mode=0o700)
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_TARGET_EXISTS",
            _error_code(lambda: self.fixture.prepare()),
        )
        (self.fixture.private_root / "generation-a").rmdir()
        review = self.fixture.prepare()
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        self.fixture.private_root.chmod(0o755)
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_PRIVATE_ROOT_STALE",
            _error_code(lambda: execute_private_delivery_write_response(response)),
        )

    def test_target_leaf_and_source_hardlink_are_rejected(self) -> None:
        for value in ("../escape", ".hidden", "bad/name", "bad\nname"):
            self.assertEqual(
                "PRIVATE_DELIVERY_WRITE_TARGET_LEAF_INVALID",
                _error_code(lambda value=value: self.fixture.prepare(target_leaf=value)),
            )
        alias = self.fixture.data_dir / "plan-alias.json"
        os.link(self.fixture.plan_path, alias)
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_SOURCE_UNAVAILABLE",
            _error_code(lambda: self.fixture.prepare()),
        )

    def test_source_and_existing_target_path_types_fail_closed(self) -> None:
        source_bytes = self.fixture.plan_path.read_bytes()
        outside = self.fixture.root / "outside-plan.json"
        outside.write_bytes(source_bytes)
        outside.chmod(0o600)
        for kind in ("symlink", "fifo", "directory"):
            if self.fixture.plan_path.exists() or self.fixture.plan_path.is_symlink():
                if self.fixture.plan_path.is_dir():
                    self.fixture.plan_path.rmdir()
                else:
                    self.fixture.plan_path.unlink()
            if kind == "symlink":
                self.fixture.plan_path.symlink_to(outside)
            elif kind == "fifo":
                os.mkfifo(self.fixture.plan_path, 0o600)
            else:
                self.fixture.plan_path.mkdir(mode=0o700)
            self.assertEqual(
                "PRIVATE_DELIVERY_WRITE_SOURCE_UNAVAILABLE",
                _error_code(lambda: self.fixture.prepare()),
            )
        self.fixture.plan_path.rmdir()
        self.fixture.plan_path.write_bytes(source_bytes)
        self.fixture.plan_path.chmod(0o600)

        targets = (
            ("target-file", "file"),
            ("target-fifo", "fifo"),
            ("target-link", "symlink"),
        )
        for leaf, kind in targets:
            path = self.fixture.private_root / leaf
            if kind == "file":
                path.write_bytes(b"synthetic")
                path.chmod(0o600)
            elif kind == "fifo":
                os.mkfifo(path, 0o600)
            else:
                path.symlink_to(outside)
            self.assertEqual(
                "PRIVATE_DELIVERY_WRITE_TARGET_EXISTS",
                _error_code(lambda leaf=leaf: self.fixture.prepare(target_leaf=leaf)),
            )

    def test_evidence_source_path_identity_is_exactly_bound(self) -> None:
        self.fixture.source.trips_root = self.fixture.root / "other-trips"
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_EVIDENCE_UNAVAILABLE",
            _error_code(lambda: self.fixture.prepare()),
        )

    def test_expiry_and_clock_rollback_are_fail_closed(self) -> None:
        review = self.fixture.prepare()
        self.fixture.clock.value = review.expires_at
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_REVIEW_EXPIRED",
            _error_code(
                lambda: capture_private_delivery_write_response(
                    review,
                    PrivateDeliveryWriteResponseKind.
                    AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
                )
            ),
        )
        self.fixture.clock.value = EVALUATION_AT
        second = self.fixture.prepare(target_leaf="generation-b")
        self.fixture.clock.value = second.created_at - timedelta(seconds=1)
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_CLOCK_ROLLBACK",
            _error_code(
                lambda: capture_private_delivery_write_response(
                    second,
                    PrivateDeliveryWriteResponseKind.CANCEL,
                )
            ),
        )

    def test_pre_manifest_fault_is_partial_and_never_claims_commit(self) -> None:
        def fault(stage: str) -> None:
            if stage == "after_payload_directory_fsync":
                raise RuntimeError(PRIVATE_SENTINEL)

        review = self.fixture.prepare(fault_hook=fault)
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        outcome = execute_private_delivery_write_response(response)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
            outcome.kind,
        )
        target = self.fixture.private_root / "generation-a"
        self.assertTrue((target / "index.html").exists())
        self.assertFalse((target / "manifest.json").exists())
        self.assertFalse(outcome.final_target_published)
        self.assertTrue(outcome.recovery_required)

    def test_post_manifest_fault_is_unknown_then_exactly_reconciled(self) -> None:
        def fault(stage: str) -> None:
            if stage == "after_manifest_fsync":
                raise RuntimeError(PRIVATE_SENTINEL)

        review = self.fixture.prepare(fault_hook=fault)
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        outcome = execute_private_delivery_write_response(response)
        self.assertIs(PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN, outcome.kind)
        reconciled = reconcile_private_delivery_write_response(response)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.RECONCILED_CREATED,
            reconciled.kind,
        )
        self.assertFalse(reconciled.recovery_required)

    def test_unknown_with_invalid_manifest_reconciles_as_conflict(self) -> None:
        def fault(stage: str) -> None:
            if stage == "after_manifest_create":
                raise RuntimeError(PRIVATE_SENTINEL)

        review = self.fixture.prepare(fault_hook=fault)
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        outcome = execute_private_delivery_write_response(response)
        self.assertIs(PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN, outcome.kind)
        conflict = reconcile_private_delivery_write_response(response)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.RECOVERY_CONFLICT,
            conflict.kind,
        )

    def test_two_reviews_for_same_leaf_never_overwrite(self) -> None:
        first = self.fixture.prepare()
        second = self.fixture.prepare()
        first_response = capture_private_delivery_write_response(
            first,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        second_response = capture_private_delivery_write_response(
            second,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        execute_private_delivery_write_response(first_response)
        before = {
            path.name: path.read_bytes()
            for path in (self.fixture.private_root / "generation-a").iterdir()
        }
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_TARGET_EXISTS",
            _error_code(
                lambda: execute_private_delivery_write_response(second_response)
            ),
        )
        after = {
            path.name: path.read_bytes()
            for path in (self.fixture.private_root / "generation-a").iterdir()
        }
        self.assertEqual(before, after)

    def test_safe_surfaces_and_errors_never_echo_private_values(self) -> None:
        self.fixture.plan["state"]["trip"]["subtitle"] = PRIVATE_SENTINEL
        self.fixture.plan["revision"] = compute_revision(self.fixture.plan)
        self.fixture.plan_path.write_bytes(encode_plan(self.fixture.plan))
        review = self.fixture.prepare()
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        for value in (
            repr(review),
            repr(response),
            json.dumps(review.to_safe_dict()),
            json.dumps(response.to_safe_dict()),
        ):
            self.assertNotIn(PRIVATE_SENTINEL, value)
            self.assertNotIn(str(self.fixture.private_root), value)
            self.assertNotIn(review.review_id, value)

    def test_safe_status_tracks_capture_execution_and_rejects_outcome_tamper(
        self,
    ) -> None:
        review = self.fixture.prepare()
        self.assertFalse(
            review.to_safe_dict()["authorization_response_captured"]
        )
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        captured = response.to_safe_dict()
        self.assertFalse(captured["response_consumed"])
        self.assertEqual("not_performed", captured["write_outcome"])
        self.assertFalse(captured["writes_performed"])
        self.assertTrue(
            review.to_safe_dict()["authorization_response_captured"]
        )

        outcome = execute_private_delivery_write_response(response)
        for safe in (
            outcome.to_safe_dict(),
            review.to_safe_dict(),
            response.to_safe_dict(),
        ):
            self.assertEqual("created", safe["write_outcome"])
            self.assertTrue(safe["final_target_published"])
            self.assertFalse(safe["cleanup_outstanding"])
        self.assertTrue(response.to_safe_dict()["response_consumed"])

        object.__setattr__(outcome, "final_target_published", False)
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_OUTCOME_TAMPERED",
            _error_code(outcome.to_safe_dict),
        )
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_OUTCOME_TAMPERED",
            _error_code(review.to_safe_dict),
        )

    def test_clock_advance_during_initial_reload_refuses_before_create(
        self,
    ) -> None:
        review = self.fixture.prepare()
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )

        def advance(read_count: int) -> None:
            if read_count == 3:
                self.fixture.clock.value = review.expires_at

        self.fixture.source.on_read = advance
        self.assertEqual(
            "PRIVATE_DELIVERY_WRITE_REVIEW_EXPIRED",
            _error_code(
                lambda: execute_private_delivery_write_response(response)
            ),
        )
        self.assertFalse(
            (self.fixture.private_root / "generation-a").exists()
        )

    def test_clock_advance_during_final_reload_leaves_no_manifest(self) -> None:
        review = self.fixture.prepare()
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )

        def advance(read_count: int) -> None:
            if read_count == 5:
                self.fixture.clock.value = review.expires_at

        self.fixture.source.on_read = advance
        outcome = execute_private_delivery_write_response(response)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
            outcome.kind,
        )
        target = self.fixture.private_root / "generation-a"
        self.assertTrue((target / "index.html").exists())
        self.assertFalse((target / "manifest.json").exists())

    def test_restrictive_owner_umask_fails_closed_without_artifacts(self) -> None:
        review = self.fixture.prepare()
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        previous = os.umask(0o777)
        try:
            outcome = execute_private_delivery_write_response(response)
        finally:
            os.umask(previous)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
            outcome.kind,
        )
        self.assertFalse(outcome.final_target_published)
        target = self.fixture.private_root / "generation-a"
        self.assertEqual(0o000, stat.S_IMODE(target.stat().st_mode))
        target.chmod(0o700)
        self.assertEqual([], list(target.iterdir()))

    def test_target_inode_swap_after_create_never_writes_replacement(self) -> None:
        moved = self.fixture.private_root / "generation-moved"
        replacement = self.fixture.private_root / "generation-a"

        def fault(stage: str) -> None:
            if stage == "after_target_create":
                replacement.rename(moved)
                replacement.mkdir(mode=0o755)
                replacement.chmod(0o755)

        review = self.fixture.prepare(fault_hook=fault)
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        outcome = execute_private_delivery_write_response(response)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
            outcome.kind,
        )
        self.assertEqual(0o755, stat.S_IMODE(replacement.stat().st_mode))
        self.assertEqual([], list(replacement.iterdir()))
        self.assertEqual([], list(moved.iterdir()))

    def test_final_target_swap_and_same_size_tamper_never_report_created(
        self,
    ) -> None:
        moved = self.fixture.private_root / "generation-moved"
        target = self.fixture.private_root / "generation-a"

        def swap(stage: str) -> None:
            if stage == "after_parent_directory_fsync":
                target.rename(moved)
                target.mkdir(mode=0o700)

        review = self.fixture.prepare(fault_hook=swap)
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        swapped = execute_private_delivery_write_response(response)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
            swapped.kind,
        )
        self.assertIsNone(swapped.final_target_published)

        second = _Fixture(self)
        try:
            second_target = second.private_root / "generation-b"

            def tamper(stage: str) -> None:
                if stage == "after_parent_directory_fsync":
                    manifest = second_target / "manifest.json"
                    payload = bytearray(manifest.read_bytes())
                    payload[0] ^= 1
                    manifest.write_bytes(payload)
                    manifest.chmod(0o600)

            second_review = second.prepare(
                target_leaf="generation-b",
                fault_hook=tamper,
            )
            second_response = capture_private_delivery_write_response(
                second_review,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
            )
            tampered = execute_private_delivery_write_response(
                second_response
            )
            self.assertIs(
                PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
                tampered.kind,
            )
        finally:
            second.cleanup()

    def test_reconcile_transient_unavailability_is_retryable(self) -> None:
        def fault(stage: str) -> None:
            if stage == "after_manifest_fsync":
                raise RuntimeError(PRIVATE_SENTINEL)

        review = self.fixture.prepare(fault_hook=fault)
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
            execute_private_delivery_write_response(response).kind,
        )
        with mock.patch.object(
            writer_module,
            "_inspect_sync_and_reinspect_target",
            return_value="unavailable",
        ):
            first = reconcile_private_delivery_write_response(response)
        self.assertIs(PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN, first.kind)
        second = reconcile_private_delivery_write_response(response)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.RECONCILED_CREATED,
            second.kind,
        )

    def test_manifest_fsync_failure_can_only_reconcile_after_file_fsync(
        self,
    ) -> None:
        review = self.fixture.prepare()
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        real_fsync = os.fsync
        failed = False

        def fail_manifest_once(descriptor: int) -> None:
            nonlocal failed
            try:
                name = os.readlink(f"/proc/self/fd/{descriptor}")
            except OSError:
                name = ""
            if not failed and name.endswith("/manifest.json"):
                failed = True
                raise OSError(PRIVATE_SENTINEL)
            real_fsync(descriptor)

        with mock.patch.object(os, "fsync", side_effect=fail_manifest_once):
            unknown = execute_private_delivery_write_response(response)
        self.assertTrue(failed)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
            unknown.kind,
        )
        reconciled = reconcile_private_delivery_write_response(response)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.RECONCILED_CREATED,
            reconciled.kind,
        )

    def test_create_syscall_lost_ack_is_never_reported_as_no_effect(self) -> None:
        review = self.fixture.prepare()
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        real_mkdir = os.mkdir

        def lost_mkdir_ack(path, mode=0o777, *, dir_fd=None):
            real_mkdir(path, mode, dir_fd=dir_fd)
            raise OSError(PRIVATE_SENTINEL)

        with mock.patch.object(os, "mkdir", side_effect=lost_mkdir_ack):
            mkdir_outcome = execute_private_delivery_write_response(response)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
            mkdir_outcome.kind,
        )
        self.assertTrue(
            (self.fixture.private_root / "generation-a").exists()
        )

        second = _Fixture(self)
        try:
            second_review = second.prepare(target_leaf="generation-b")
            second_response = capture_private_delivery_write_response(
                second_review,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
            )
            real_open = writer_module._open_new_artifact

            def lost_manifest_ack(target_fd: int, filename: str) -> int:
                descriptor = real_open(target_fd, filename)
                if filename == "manifest.json":
                    os.close(descriptor)
                    raise RuntimeError(PRIVATE_SENTINEL)
                return descriptor

            with mock.patch.object(
                writer_module,
                "_open_new_artifact",
                side_effect=lost_manifest_ack,
            ):
                manifest_outcome = execute_private_delivery_write_response(
                    second_response
                )
            self.assertIs(
                PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
                manifest_outcome.kind,
            )
            self.assertIsNone(manifest_outcome.final_target_published)
            self.assertTrue(
                (
                    second.private_root
                    / "generation-b"
                    / "manifest.json"
                ).exists()
            )
        finally:
            second.cleanup()

    def test_reconcile_stable_extra_entry_is_terminal_conflict(self) -> None:
        def fault(stage: str) -> None:
            if stage == "after_manifest_fsync":
                raise RuntimeError(PRIVATE_SENTINEL)

        review = self.fixture.prepare(fault_hook=fault)
        response = capture_private_delivery_write_response(
            review,
            PrivateDeliveryWriteResponseKind.
            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
        )
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
            execute_private_delivery_write_response(response).kind,
        )
        extra = self.fixture.private_root / "generation-a" / "extra.bin"
        extra.write_bytes(b"synthetic")
        extra.chmod(0o600)
        conflict = reconcile_private_delivery_write_response(response)
        self.assertIs(
            PrivateDeliveryWriteOutcomeKind.RECOVERY_CONFLICT,
            conflict.kind,
        )

    def test_evidence_store_read_snapshot_is_read_only(self) -> None:
        evidence = EvidenceStore(
            self.fixture.trips_root,
            self.fixture.slug,
            self.fixture.plan["trip_id"],
            self.fixture.snapshot.policies,
            clock=self.fixture.clock,
        )
        cache = self.fixture.data_dir / "evidence.json"
        lock = self.fixture.data_dir / ".trip-planner.lock"
        before = sorted(path.name for path in self.fixture.data_dir.iterdir())
        snapshot = evidence.read_snapshot(evaluation_at=EVALUATION_AT)
        after = sorted(path.name for path in self.fixture.data_dir.iterdir())
        self.assertEqual(before, after)
        self.assertFalse(cache.exists())
        self.assertFalse(lock.exists())
        self.assertEqual(EVALUATION_AT, snapshot.evaluation_at)

    def test_existing_evidence_read_prunes_only_memory_and_keeps_orphan(
        self,
    ) -> None:
        policies = policy_registry(disk_retention_seconds=3600)
        retrieved_at = EVALUATION_AT - timedelta(hours=2)
        evidence = EvidenceStore(
            self.fixture.trips_root,
            self.fixture.slug,
            self.fixture.plan["trip_id"],
            policies,
            clock=MutableClock(retrieved_at),
        )
        observation = route_observation(
            policies,
            route_key(),
            retrieved_at=retrieved_at,
            valid_until=retrieved_at + timedelta(minutes=30),
            purge_at=retrieved_at + timedelta(hours=1),
        )
        merged = evidence.merge(authorized_success(policies, observation))
        self.assertTrue(merged.success)
        cache = evidence.cache_path
        orphan = (
            self.fixture.data_dir
            / ".trip-planner-evidence.synthetic.tmp"
        )
        orphan.write_bytes(b"synthetic orphan")
        orphan.chmod(0o600)
        before_cache = cache.read_bytes()
        before_orphan = orphan.read_bytes()
        before_lock = evidence.lock_path.read_bytes()
        before_names = sorted(path.name for path in self.fixture.data_dir.iterdir())

        snapshot = evidence.read_snapshot(evaluation_at=EVALUATION_AT)

        self.assertEqual((), snapshot.observations)
        self.assertEqual(merged.current_revision, snapshot.store_revision)
        self.assertEqual(before_cache, cache.read_bytes())
        self.assertEqual(before_orphan, orphan.read_bytes())
        self.assertEqual(
            before_names,
            sorted(path.name for path in self.fixture.data_dir.iterdir()),
        )
        self.assertEqual(before_lock, evidence.lock_path.read_bytes())

    def test_readonly_evidence_corruption_refuses_without_repair(self) -> None:
        policies = policy_registry()
        evidence = EvidenceStore(
            self.fixture.trips_root,
            self.fixture.slug,
            self.fixture.plan["trip_id"],
            policies,
            clock=MutableClock(EVALUATION_AT),
        )
        observation = route_observation(
            policies,
            route_key(),
            retrieved_at=EVALUATION_AT,
        )
        merged = evidence.merge(authorized_success(policies, observation))
        self.assertTrue(merged.success)
        document = json.loads(evidence.cache_path.read_bytes())
        document["store_revision"] = "0" * 64
        tampered = (
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        evidence.cache_path.write_bytes(tampered)
        evidence.cache_path.chmod(0o600)
        before_names = sorted(path.name for path in self.fixture.data_dir.iterdir())
        with self.assertRaises(EvidenceStoreError) as caught:
            evidence.read_snapshot(evaluation_at=EVALUATION_AT)
        self.assertEqual("EVIDENCE_READONLY_UNAVAILABLE", caught.exception.code)
        self.assertEqual(tampered, evidence.cache_path.read_bytes())
        self.assertEqual(
            before_names,
            sorted(path.name for path in self.fixture.data_dir.iterdir()),
        )


if __name__ == "__main__":
    unittest.main()
