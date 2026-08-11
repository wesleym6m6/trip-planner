"""Synthetic Phase 6.2D EvidenceSession-backed writer execution tests."""

from __future__ import annotations

import json
import stat
import threading
import unittest
from datetime import timedelta
from unittest import mock

from trip_planner.evidence_session import EvidenceSessionDeliverySource
from trip_planner.evidence_store import EvidenceStore
from trip_planner.private_delivery import PrivateDeliveryProfile
from trip_planner.private_delivery_writer import (
    PrivateDeliveryWriteError,
    PrivateDeliveryWriteOutcomeKind,
    PrivateDeliveryWriteResponseKind,
    capture_private_delivery_write_response,
    execute_private_delivery_write_response,
    prepare_private_delivery_write_review,
)
from tests.test_phase4_composition import EVALUATION_AT, _route_key
from tests.test_phase62b_private_delivery_writer import (
    _Fixture as _GenericWriterFixture,
)
from tests.test_phase62c_evidence_session_delivery import (
    PRIVATE_SENTINEL,
    _Clock,
    _Fixture,
    _authorized_routes,
)


def _error_code(callable_object) -> str:
    with unittest.TestCase().assertRaises(
        PrivateDeliveryWriteError
    ) as caught:
        callable_object()
    return caught.exception.code


def _prepare(
    fixture: _Fixture,
    *,
    profile: PrivateDeliveryProfile = PrivateDeliveryProfile.HTML_PREVIEW,
    target_leaf: str,
    clock,
    fault_hook=None,
):
    kwargs = {}
    if profile is PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE:
        kwargs["lodging_intake"] = fixture.lodging_intake
    return prepare_private_delivery_write_review(
        fixture.store,
        fixture.adapter,
        profile=profile,
        private_root=fixture.private_root,
        target_leaf=target_leaf,
        clock=clock,
        fault_hook=fault_hook,
        **kwargs,
    )


class _ReentrantReviewClock:
    def __init__(self) -> None:
        self.value = EVALUATION_AT
        self.review = None
        self.response = None
        self.armed = False
        self._inside = False

    def __call__(self):
        if self.armed and not self._inside:
            self._inside = True
            try:
                self.review.to_safe_dict()
                if self.response is not None:
                    self.response.to_safe_dict()
            finally:
                self._inside = False
        return self.value


class _OutcomeDriftClock:
    def __init__(self, mutate) -> None:
        self.value = EVALUATION_AT
        self.calls = 0
        self._mutate = mutate

    def __call__(self):
        self.calls += 1
        if self.calls == 5:
            self._mutate()
        return self.value


class _NestedAuthorizationClock:
    def __init__(self) -> None:
        self.value = EVALUATION_AT
        self.review = None
        self.armed = False
        self._inside = False
        self.nested_errors: list[str] = []

    def __call__(self):
        if self.armed and not self._inside:
            self._inside = True
            try:
                try:
                    capture_private_delivery_write_response(
                        self.review,
                        PrivateDeliveryWriteResponseKind.
                        AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
                    )
                except PrivateDeliveryWriteError as exc:
                    self.nested_errors.append(exc.code)
            finally:
                self._inside = False
        return self.value


class Phase62DEvidenceSessionDeliveryWriteTests(unittest.TestCase):
    def test_both_profiles_execute_exact_reviewed_synthetic_bundles(
        self,
    ) -> None:
        cases = (
            (
                PrivateDeliveryProfile.HTML_PREVIEW,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
                "execute-preview",
                {"index.html", "manifest.json"},
            ),
            (
                PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_ICS_READY_BUNDLE_CREATE_ONLY_WRITE,
                "execute-ready",
                {"index.html", "calendar.ics", "manifest.json"},
            ),
        )
        for profile, kind, leaf, expected_names in cases:
            with self.subTest(profile=profile.value):
                fixture = _Fixture()
                try:
                    ledger_before = fixture.session._ledger
                    revision_before = fixture.session._store_revision
                    problems_before = dict(fixture.session._problems)
                    endpoints_before = set(
                        fixture.session._accepted_place_details_bases
                    )
                    floor_before = fixture.session._clock.high_water()
                    cache_before = fixture.evidence_store.cache_path.read_bytes()
                    cache_stat_before = fixture.evidence_store.cache_path.stat()
                    plan_before = fixture.plan_path.read_bytes()
                    inventory_before = tuple(
                        sorted(item.name for item in fixture.data_dir.iterdir())
                    )
                    clock = _Clock(EVALUATION_AT)
                    with mock.patch.object(
                        EvidenceStore,
                        "load",
                        side_effect=AssertionError(PRIVATE_SENTINEL),
                    ):
                        review = _prepare(
                            fixture,
                            profile=profile,
                            target_leaf=leaf,
                            clock=clock,
                        )
                        response = capture_private_delivery_write_response(
                            review,
                            kind,
                        )
                        outcome = execute_private_delivery_write_response(
                            response
                        )

                    self.assertIs(
                        PrivateDeliveryWriteOutcomeKind.CREATED,
                        outcome.kind,
                    )
                    self.assertTrue(outcome.final_target_published)
                    target = fixture.private_root / leaf
                    self.assertEqual(
                        expected_names,
                        {item.name for item in target.iterdir()},
                    )
                    self.assertEqual(0o700, stat.S_IMODE(target.stat().st_mode))
                    artifacts = {
                        item.filename: item for item in review.artifacts
                    }
                    for name, artifact in artifacts.items():
                        path = target / name
                        self.assertEqual(
                            0o600,
                            stat.S_IMODE(path.stat().st_mode),
                        )
                        self.assertEqual(artifact.payload, path.read_bytes())
                    if profile is PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE:
                        manifest = json.loads(
                            (target / "manifest.json").read_bytes()
                        )
                        self.assertEqual(
                            "travel_ready",
                            manifest["readiness"]["status"],
                        )

                    self.assertIs(ledger_before, fixture.session._ledger)
                    self.assertEqual(
                        revision_before,
                        fixture.session._store_revision,
                    )
                    self.assertEqual(problems_before, fixture.session._problems)
                    self.assertEqual(
                        endpoints_before,
                        fixture.session._accepted_place_details_bases,
                    )
                    self.assertEqual(
                        floor_before,
                        fixture.session._clock.high_water(),
                    )
                    self.assertEqual(plan_before, fixture.plan_path.read_bytes())
                    self.assertEqual(
                        cache_before,
                        fixture.evidence_store.cache_path.read_bytes(),
                    )
                    cache_stat_after = fixture.evidence_store.cache_path.stat()
                    self.assertEqual(
                        cache_stat_before.st_ino,
                        cache_stat_after.st_ino,
                    )
                    self.assertEqual(
                        cache_stat_before.st_size,
                        cache_stat_after.st_size,
                    )
                    self.assertEqual(
                        cache_stat_before.st_mtime_ns,
                        cache_stat_after.st_mtime_ns,
                    )
                    self.assertEqual(
                        inventory_before,
                        tuple(
                            sorted(
                                item.name
                                for item in fixture.data_dir.iterdir()
                            )
                        ),
                    )
                finally:
                    fixture.cleanup()

    def test_prewrite_memory_outcome_drift_refuses_before_target(self) -> None:
        fixture = _Fixture()
        try:
            leaf = "prewrite-memory-drift"
            review = _prepare(
                fixture,
                target_leaf=leaf,
                clock=_Clock(EVALUATION_AT),
            )
            response = capture_private_delivery_write_response(
                review,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
            )
            before = fixture.adapter.read_snapshot(
                evaluation_at=EVALUATION_AT
            )
            fixture.session.merge(
                _authorized_routes(fixture.registry, failed=True)
            )
            after = fixture.adapter.read_snapshot(
                evaluation_at=EVALUATION_AT
            )
            self.assertEqual(before.observations, after.observations)
            self.assertEqual(
                before.evidence_revision,
                after.evidence_revision,
            )
            self.assertNotEqual(
                before.outcome_revision,
                after.outcome_revision,
            )
            self.assertEqual(
                "PRIVATE_DELIVERY_WRITE_REVIEW_STALE",
                _error_code(
                    lambda: execute_private_delivery_write_response(response)
                ),
            )
            self.assertFalse((fixture.private_root / leaf).exists())
        finally:
            fixture.cleanup()

    def test_midwrite_memory_outcome_drift_leaves_no_manifest(self) -> None:
        fixture = _Fixture()
        try:
            leaf = "midwrite-memory-drift"
            injected: list[str] = []

            def fault(stage: str) -> None:
                if stage == "after_payload_directory_fsync":
                    fixture.session.merge(
                        _authorized_routes(fixture.registry, failed=True)
                    )
                    injected.append(stage)

            review = _prepare(
                fixture,
                target_leaf=leaf,
                clock=_Clock(EVALUATION_AT),
                fault_hook=fault,
            )
            response = capture_private_delivery_write_response(
                review,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
            )
            outcome = execute_private_delivery_write_response(response)
            self.assertEqual(["after_payload_directory_fsync"], injected)
            self.assertIs(
                PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
                outcome.kind,
            )
            self.assertFalse(outcome.final_target_published)
            target = fixture.private_root / leaf
            self.assertTrue((target / "index.html").exists())
            self.assertFalse((target / "manifest.json").exists())
        finally:
            fixture.cleanup()

    def test_final_clock_callback_drift_is_reproduced_before_manifest(
        self,
    ) -> None:
        fixture = _Fixture()
        try:
            leaf = "final-clock-drift"
            clock = _OutcomeDriftClock(
                lambda: fixture.session.merge(
                    _authorized_routes(fixture.registry, failed=True)
                )
            )
            review = _prepare(
                fixture,
                target_leaf=leaf,
                clock=clock,
            )
            response = capture_private_delivery_write_response(
                review,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
            )
            outcome = execute_private_delivery_write_response(response)
            self.assertEqual(5, clock.calls)
            self.assertIs(
                PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
                outcome.kind,
            )
            self.assertFalse(outcome.final_target_published)
            target = fixture.private_root / leaf
            self.assertTrue((target / "index.html").exists())
            self.assertFalse((target / "manifest.json").exists())
        finally:
            fixture.cleanup()

    def test_reentrant_clock_safe_view_does_not_deadlock_or_recapture(
        self,
    ) -> None:
        fixture = _Fixture()
        try:
            leaf = "reentrant-clock"
            clock = _ReentrantReviewClock()
            review = _prepare(
                fixture,
                target_leaf=leaf,
                clock=clock,
            )
            clock.review = review
            clock.armed = True
            result: dict[str, object] = {}

            def capture() -> None:
                try:
                    result["response"] = (
                        capture_private_delivery_write_response(
                            review,
                            PrivateDeliveryWriteResponseKind.
                            AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
                        )
                    )
                except Exception as exc:
                    result["error"] = exc

            thread = threading.Thread(target=capture, daemon=True)
            thread.start()
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive(), "reentrant clock deadlocked")
            self.assertNotIn("error", result)
            self.assertIn("response", result)
            response = result["response"]
            self.assertEqual(
                "PRIVATE_DELIVERY_WRITE_RESPONSE_ALREADY_CAPTURED",
                _error_code(
                    lambda: capture_private_delivery_write_response(
                        review,
                        PrivateDeliveryWriteResponseKind.CANCEL,
                    )
                ),
            )
            clock.response = response
            outcome = execute_private_delivery_write_response(response)
            self.assertIs(
                PrivateDeliveryWriteOutcomeKind.CREATED,
                outcome.kind,
            )
            self.assertTrue((fixture.private_root / leaf).exists())
        finally:
            fixture.cleanup()

    def test_reentrant_clock_cannot_replace_cancel_with_authorization(
        self,
    ) -> None:
        fixture = _Fixture()
        try:
            leaf = "reentrant-authorization"
            clock = _NestedAuthorizationClock()
            review = _prepare(
                fixture,
                target_leaf=leaf,
                clock=clock,
            )
            clock.review = review
            clock.armed = True
            self.assertEqual(
                "PRIVATE_DELIVERY_WRITE_REENTRANT_OPERATION",
                _error_code(
                    lambda: capture_private_delivery_write_response(
                        review,
                        PrivateDeliveryWriteResponseKind.CANCEL,
                    )
                ),
            )
            self.assertEqual(
                ["PRIVATE_DELIVERY_WRITE_REENTRANT_OPERATION"],
                clock.nested_errors,
            )
            self.assertFalse(
                review.to_safe_dict()["authorization_response_captured"]
            )
            self.assertFalse((fixture.private_root / leaf).exists())

            clock.armed = False
            response = capture_private_delivery_write_response(
                review,
                PrivateDeliveryWriteResponseKind.CANCEL,
            )
            outcome = execute_private_delivery_write_response(response)
            self.assertIs(
                PrivateDeliveryWriteOutcomeKind.CANCELLED,
                outcome.kind,
            )
            self.assertFalse((fixture.private_root / leaf).exists())
        finally:
            fixture.cleanup()

    def test_durable_drift_and_adapter_tamper_are_no_write_failures(
        self,
    ) -> None:
        fixture = _Fixture()
        try:
            leaf = "durable-drift"
            ledger_before = fixture.session._ledger
            revision_before = fixture.session._store_revision
            review = _prepare(
                fixture,
                target_leaf=leaf,
                clock=_Clock(EVALUATION_AT),
            )
            response = capture_private_delivery_write_response(
                review,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
            )
            fixture.evidence_store.merge(
                _authorized_routes(
                    fixture.registry,
                    provider="disk-route",
                    specs=(
                        (
                            _route_key("durable-b", "durable-a"),
                            24.0,
                            EVALUATION_AT + timedelta(hours=2),
                        ),
                    ),
                )
            )
            cache_after_drift = fixture.evidence_store.cache_path.read_bytes()
            self.assertEqual(
                "PRIVATE_DELIVERY_WRITE_EVIDENCE_UNAVAILABLE",
                _error_code(
                    lambda: execute_private_delivery_write_response(response)
                ),
            )
            self.assertFalse((fixture.private_root / leaf).exists())
            self.assertIs(ledger_before, fixture.session._ledger)
            self.assertEqual(revision_before, fixture.session._store_revision)
            self.assertEqual(
                cache_after_drift,
                fixture.evidence_store.cache_path.read_bytes(),
            )
        finally:
            fixture.cleanup()

        fixture = _Fixture()
        try:
            leaf = "adapter-tamper"
            review = _prepare(
                fixture,
                target_leaf=leaf,
                clock=_Clock(EVALUATION_AT),
            )
            response = capture_private_delivery_write_response(
                review,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
            )
            object.__setattr__(fixture.adapter, "_seal", "0" * 64)
            self.assertEqual(
                "PRIVATE_DELIVERY_WRITE_EVIDENCE_UNAVAILABLE",
                _error_code(
                    lambda: execute_private_delivery_write_response(response)
                ),
            )
            self.assertFalse((fixture.private_root / leaf).exists())
        finally:
            fixture.cleanup()

    def test_generic_source_reentry_preserves_partial_effect_truth(
        self,
    ) -> None:
        fixture = _GenericWriterFixture(self)
        try:
            review = fixture.prepare(target_leaf="generic-reentry")
            response = capture_private_delivery_write_response(
                review,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
            )
            nested_errors: list[str] = []

            def reenter(read_count: int) -> None:
                if read_count == 9:
                    try:
                        execute_private_delivery_write_response(response)
                    except PrivateDeliveryWriteError as exc:
                        nested_errors.append(exc.code)

            fixture.source.on_read = reenter
            outcome = execute_private_delivery_write_response(response)
            self.assertEqual(
                ["PRIVATE_DELIVERY_WRITE_REENTRANT_OPERATION"],
                nested_errors,
            )
            self.assertIs(
                PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
                outcome.kind,
            )
            target = fixture.private_root / "generic-reentry"
            self.assertTrue((target / "index.html").exists())
            self.assertFalse((target / "manifest.json").exists())
            safe = response.to_safe_dict()
            self.assertTrue(safe["writes_performed"])
            self.assertEqual("partial_uncommitted", safe["write_outcome"])
        finally:
            fixture.cleanup()

    def test_writer_pins_exact_adapter_method_dispatch(self) -> None:
        fixture = _Fixture()
        try:
            calls: list[str] = []

            def hostile(*args, **kwargs):
                del args, kwargs
                calls.append(PRIVATE_SENTINEL)
                raise RuntimeError(PRIVATE_SENTINEL)

            with mock.patch.object(
                EvidenceSessionDeliverySource,
                "read_snapshot",
                side_effect=hostile,
            ):
                review = _prepare(
                    fixture,
                    target_leaf="pinned-adapter-dispatch",
                    clock=_Clock(EVALUATION_AT),
                )
            self.assertEqual([], calls)
            self.assertFalse(
                (fixture.private_root / "pinned-adapter-dispatch").exists()
            )
            self.assertFalse(
                review.to_safe_dict()["authorization_response_captured"]
            )
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
