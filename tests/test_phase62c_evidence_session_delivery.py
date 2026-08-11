"""Synthetic Phase 6.2C MEMORY_ONLY delivery-source integration tests."""

from __future__ import annotations

import pickle
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import trip_planner
import trip_planner.evidence_session as evidence_session_module

from trip_planner.codec import encode_plan
from trip_planner.evidence_session import (
    EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION,
    EvidenceSession,
    EvidenceSessionDeliverySource,
)
from trip_planner.evidence_store import EvidenceStore
from trip_planner.facts import (
    EvidenceLedger,
    EvidencePersistence,
    EvidenceSnapshot,
    FactContractError,
    FactKind,
    FactObservation,
    FactValue,
    ProviderPolicy,
    ProviderPolicyRegistry,
    ProviderProblem,
    ProviderProblemCode,
    ProviderProvenance,
    ProviderRequest,
    ProviderResult,
    ProviderResultStatus,
    authorize_provider_result,
)
from trip_planner.lodging import LodgingRequirement, assess_lodging_intake
from trip_planner.private_delivery import PrivateDeliveryProfile
from trip_planner.private_delivery_writer import (
    prepare_private_delivery_write_review,
)
from trip_planner.store import TripStore
from tests.test_phase4_composition import (
    ATTRIBUTION_LABEL,
    ATTRIBUTION_URI,
    COMPLETED_AT,
    EVALUATION_AT,
    NOW,
    _canonical_plan,
    _policies,
    _route_key,
)
from tests.test_phase4_evidence_session import MutableSource, base_result


PRIVATE_SENTINEL = "SYNTHETIC-PRIVATE-SESSION-DELIVERY-SENTINEL"


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _HostileClock:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def high_water(self) -> datetime:
        self._calls.append(PRIVATE_SENTINEL)
        raise RuntimeError(PRIVATE_SENTINEL)


class _HostileComparable:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def __lt__(self, other: object) -> bool:
        del other
        self._calls.append(PRIVATE_SENTINEL)
        raise RuntimeError(PRIVATE_SENTINEL)

    def __gt__(self, other: object) -> bool:
        del other
        self._calls.append(PRIVATE_SENTINEL)
        raise RuntimeError(PRIVATE_SENTINEL)


class _HostileProblemCode:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    @property
    def value(self) -> str:
        self._calls.append(PRIVATE_SENTINEL)
        raise RuntimeError(PRIVATE_SENTINEL)


class _HostileDigestComparison:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def __eq__(self, other: object) -> bool:
        del other
        self._calls.append(PRIVATE_SENTINEL)
        raise RuntimeError(PRIVATE_SENTINEL)

    def __ne__(self, other: object) -> bool:
        del other
        self._calls.append(PRIVATE_SENTINEL)
        raise RuntimeError(PRIVATE_SENTINEL)


class _HostilePolicies:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    @property
    def revision(self) -> str:
        self._calls.append(PRIVATE_SENTINEL)
        raise RuntimeError(PRIVATE_SENTINEL)


class _HostileLock:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def __enter__(self) -> object:
        self._calls.append(PRIVATE_SENTINEL)
        raise RuntimeError(PRIVATE_SENTINEL)

    def __exit__(self, *args: object) -> None:
        del args
        self._calls.append(PRIVATE_SENTINEL)


class _HostileDictKey:
    def __init__(self, calls: list[str], collision: str) -> None:
        self._calls = calls
        self._collision = collision

    def __hash__(self) -> int:
        return hash(self._collision)

    def __eq__(self, other: object) -> bool:
        del other
        self._calls.append(PRIVATE_SENTINEL)
        raise RuntimeError(PRIVATE_SENTINEL)


def _authorized_routes(
    registry: ProviderPolicyRegistry,
    *,
    provider: str = "route-a",
    specs=None,
    failed: bool = False,
):
    policy = next(
        item for item in registry.policies if item.provider_id == provider
    )
    if specs is None:
        specs = (
            (
                _route_key("loc-a", "loc-b"),
                37.25,
                EVALUATION_AT + timedelta(hours=2),
            ),
            (
                _route_key("loc-b", "loc-a"),
                40.0,
                EVALUATION_AT + timedelta(hours=1),
            ),
        )
    request = ProviderRequest(
        provider_id=provider,
        adapter_id=provider,
        adapter_version="v1",
        operation="compute-route",
        fact_keys=tuple(item[0] for item in specs),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
    )
    observations = () if failed else tuple(
        FactObservation(
            key=key,
            value=FactValue.from_payload(
                FactKind.ROUTE_ESTIMATE,
                {
                    "mode": key.qualifier_map["mode"],
                    "duration_min": duration,
                    "distance_km": duration / 10,
                },
            ),
            provenance=ProviderProvenance(
                provider_id=provider,
                adapter_id=provider,
                adapter_version="v1",
                request_fingerprint=request.request_fingerprint,
                retention_policy_id=policy.policy_id,
                response_id=f"synthetic-memory-response-{index}",
                source_uri="https://example.test/synthetic-memory-source",
                attributions=((ATTRIBUTION_LABEL, ATTRIBUTION_URI),),
            ),
            retrieved_at=NOW,
            valid_until=valid_until,
            purge_at=NOW + timedelta(hours=23),
            confidence=1.0,
        )
        for index, (key, duration, valid_until) in enumerate(specs)
    )
    problems = (
        (
            ProviderProblem(
                code=ProviderProblemCode.TIMEOUT,
                message="The synthetic memory route timed out.",
                retryable=True,
                next_action="retry_with_budget",
                fact_key_ids=tuple(item[0].key_id for item in specs),
            ),
        )
        if failed
        else ()
    )
    result = ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=(
            ProviderResultStatus.FAILED
            if failed
            else ProviderResultStatus.SUCCESS
        ),
        observations=observations,
        problems=problems,
        attempts_used=1,
        completed_at=COMPLETED_AT,
    )
    return authorize_provider_result(request, result, registry)


class _Fixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.trips_root = self.root / "trips"
        self.slug = "composition-trip"
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
        self.plan = _canonical_plan()
        self.plan_path = self.data_dir / "plan.json"
        self.plan_path.write_bytes(encode_plan(self.plan))
        self.plan_path.chmod(0o600)
        memory_policy = _policies("route-a").policies[0]
        durable_policy = ProviderPolicy(
            policy_id="disk-route-runtime-v1",
            provider_id="disk-route",
            adapter_id="disk-route",
            adapter_version="v1",
            contract_region="test",
            allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
            allowed_value_fields=(
                "distance_km",
                "duration_min",
                "mode",
            ),
            allowed_operations=("compute-route",),
            persistence=EvidencePersistence.DISK_TTL,
            max_validity_seconds=12 * 60 * 60,
            max_retention_seconds=24 * 60 * 60,
            required_attribution_labels=(ATTRIBUTION_LABEL,),
        )
        self.registry = ProviderPolicyRegistry(
            policies=(memory_policy, durable_policy)
        )
        self.clock = _Clock(COMPLETED_AT)
        self.evidence_store = EvidenceStore(
            self.trips_root,
            self.slug,
            self.plan["trip_id"],
            self.registry,
            clock=self.clock,
        )
        self.evidence_store.merge(
            _authorized_routes(
                self.registry,
                provider="disk-route",
                specs=(
                    (
                        _route_key("durable-a", "durable-b"),
                        21.0,
                        EVALUATION_AT + timedelta(hours=2),
                    ),
                ),
            )
        )
        self.session = EvidenceSession(
            self.evidence_store,
            clock=self.clock,
        )
        self.session.merge(_authorized_routes(self.registry))
        self.adapter = self.session.private_delivery_source()
        self.store = TripStore(self.trips_root, self.slug)
        self.lodging_intake = assess_lodging_intake(
            stay_start=date(2026, 10, 1),
            stay_end=date(2026, 10, 2),
            requirement=LodgingRequirement.NOT_REQUIRED,
        )

    def cleanup(self) -> None:
        self.temp.cleanup()


def _error_code(callable_object) -> str:
    with unittest.TestCase().assertRaises(FactContractError) as caught:
        callable_object()
    return caught.exception.code


class Phase62CEvidenceSessionDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _Fixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_adapter_replays_memory_facts_without_load_or_session_mutation(
        self,
    ) -> None:
        ledger_before = self.fixture.session._ledger
        revision_before = self.fixture.session._store_revision
        problems_before = dict(self.fixture.session._problems)
        endpoints_before = set(
            self.fixture.session._accepted_place_details_bases
        )
        floor_before = self.fixture.session._clock.high_water()
        cache_before = self.fixture.evidence_store.cache_path.read_bytes()
        cache_stat_before = self.fixture.evidence_store.cache_path.stat()
        inventory_before = tuple(
            sorted(item.name for item in self.fixture.data_dir.iterdir())
        )
        with mock.patch.object(
            EvidenceStore,
            "load",
            side_effect=AssertionError(PRIVATE_SENTINEL),
        ):
            first = self.fixture.adapter.read_snapshot(
                evaluation_at=EVALUATION_AT
            )
            second = self.fixture.adapter.read_snapshot(
                evaluation_at=EVALUATION_AT
            )
        self.assertEqual(first, second)
        self.assertEqual(3, len(first.observations))
        self.assertEqual(
            {EvidencePersistence.DISK_TTL, EvidencePersistence.MEMORY_ONLY},
            {
                first.policies.persistence_for(item)
                for item in first.observations
            },
        )
        self.assertIs(ledger_before, self.fixture.session._ledger)
        self.assertEqual(revision_before, self.fixture.session._store_revision)
        self.assertEqual(problems_before, self.fixture.session._problems)
        self.assertEqual(
            endpoints_before,
            self.fixture.session._accepted_place_details_bases,
        )
        self.assertEqual(floor_before, self.fixture.session._clock.high_water())
        self.assertEqual(
            cache_before,
            self.fixture.evidence_store.cache_path.read_bytes(),
        )
        cache_stat_after = self.fixture.evidence_store.cache_path.stat()
        self.assertEqual(cache_stat_before.st_ino, cache_stat_after.st_ino)
        self.assertEqual(cache_stat_before.st_size, cache_stat_after.st_size)
        self.assertEqual(cache_stat_before.st_mtime_ns, cache_stat_after.st_mtime_ns)
        self.assertEqual(
            inventory_before,
            tuple(sorted(item.name for item in self.fixture.data_dir.iterdir())),
        )

    def test_only_exact_evidence_store_backing_can_create_adapter(self) -> None:
        session = EvidenceSession(
            MutableSource(base_result(self.fixture.registry)),
            clock=self.fixture.clock,
        )
        self.assertEqual(
            "EVIDENCE_DELIVERY_SOURCE_UNAVAILABLE",
            _error_code(session.private_delivery_source),
        )
        with self.assertRaises(FactContractError):
            EvidenceSessionDeliverySource(
                session=self.fixture.session,
                source=self.fixture.evidence_store,
            )

    def test_adapter_safe_surface_is_value_free_sealed_and_process_local(
        self,
    ) -> None:
        safe = self.fixture.adapter.to_safe_dict()
        self.assertEqual(
            {
                "contract_version",
                "contains_private_data",
                "ordinary_read_may_update_host_atime",
                "read_only_snapshot_source",
                "read_performs_application_level_durable_write",
                "read_performs_artifact_write",
                "read_performs_provider_call",
                "write_authorized",
            },
            set(safe),
        )
        self.assertEqual(
            EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION,
            safe["contract_version"],
        )
        self.assertFalse(safe["read_performs_provider_call"])
        self.assertFalse(
            safe["read_performs_application_level_durable_write"]
        )
        self.assertFalse(safe["read_performs_artifact_write"])
        self.assertTrue(safe["ordinary_read_may_update_host_atime"])
        self.assertFalse(safe["write_authorized"])
        safe_text = repr(safe) + repr(self.fixture.adapter)
        for private in (
            self.fixture.plan["trip_id"],
            self.fixture.slug,
            str(self.fixture.trips_root),
            str(self.fixture.data_dir),
            PRIVATE_SENTINEL,
        ):
            self.assertNotIn(private, safe_text)
        with self.assertRaises(TypeError):
            pickle.dumps(self.fixture.adapter)
        object.__setattr__(self.fixture.adapter, "slug", PRIVATE_SENTINEL)
        self.assertEqual(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            _error_code(self.fixture.adapter.to_safe_dict),
        )

    def test_public_exports_are_exact(self) -> None:
        self.assertIs(
            EvidenceSessionDeliverySource,
            trip_planner.EvidenceSessionDeliverySource,
        )
        self.assertEqual(
            EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION,
            trip_planner.EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION,
        )

    def test_durable_drift_fails_without_rebasing_memory_state(self) -> None:
        ledger_before = self.fixture.session._ledger
        revision_before = self.fixture.session._store_revision
        changed = EvidenceSnapshot.from_ledger(
            EvidenceLedger(self.fixture.registry),
            evaluation_at=EVALUATION_AT,
            purge_now=EVALUATION_AT,
            store_revision="f" * 64,
        )
        with mock.patch.object(
            evidence_session_module,
            "_EVIDENCE_STORE_READ_SNAPSHOT",
            return_value=changed,
        ):
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_STALE",
                _error_code(
                    lambda: self.fixture.adapter.read_snapshot(
                        evaluation_at=EVALUATION_AT
                    )
                ),
            )
        self.assertIs(ledger_before, self.fixture.session._ledger)
        self.assertEqual(revision_before, self.fixture.session._store_revision)

    def test_hostile_durable_snapshot_is_preflighted_before_comparison(
        self,
    ) -> None:
        durable = self.fixture.evidence_store.read_snapshot(
            evaluation_at=EVALUATION_AT
        )
        calls: list[str] = []
        object.__setattr__(
            durable,
            "store_revision",
            _HostileDigestComparison(calls),
        )
        with mock.patch.object(
            evidence_session_module,
            "_EVIDENCE_STORE_READ_SNAPSHOT",
            return_value=durable,
        ):
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_UNAVAILABLE",
                _error_code(
                    lambda: self.fixture.adapter.read_snapshot(
                        evaluation_at=EVALUATION_AT
                    )
                ),
            )
        self.assertEqual([], calls)

    def test_clock_rollback_and_nonexact_time_fail_without_advancing_clock(
        self,
    ) -> None:
        floor = self.fixture.session._clock.high_water()
        self.assertEqual(
            "EVIDENCE_DELIVERY_SOURCE_CLOCK_ROLLBACK",
            _error_code(
                lambda: self.fixture.adapter.read_snapshot(evaluation_at=NOW)
            ),
        )
        self.assertEqual(
            "EVIDENCE_DELIVERY_SOURCE_TIME_INVALID",
            _error_code(
                lambda: self.fixture.adapter.read_snapshot(
                    evaluation_at=datetime(2026, 7, 28, 10)
                )
            ),
        )
        self.assertEqual(floor, self.fixture.session._clock.high_water())

    def test_all_backing_store_paths_are_sealed(self) -> None:
        for name in (
            "trips_root",
            "trip_dir",
            "data_dir",
            "cache_path",
            "lock_path",
        ):
            original = getattr(self.fixture.evidence_store, name)
            try:
                setattr(
                    self.fixture.evidence_store,
                    name,
                    self.fixture.root / f"synthetic-wrong-{name}",
                )
                self.assertEqual(
                    "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                    _error_code(
                        lambda: self.fixture.adapter.read_snapshot(
                            evaluation_at=EVALUATION_AT
                        )
                    ),
                )
            finally:
                setattr(self.fixture.evidence_store, name, original)

    def test_retention_changes_only_the_returned_view(self) -> None:
        ledger_before = self.fixture.session._ledger
        expired = self.fixture.adapter.read_snapshot(
            evaluation_at=NOW + timedelta(days=2)
        )
        self.assertEqual((), expired.observations)
        self.assertIs(ledger_before, self.fixture.session._ledger)
        self.assertEqual(3, len(self.fixture.session._ledger.observations))

    def test_provider_outcome_drift_changes_only_exact_outcome_binding(self) -> None:
        before = self.fixture.adapter.read_snapshot(
            evaluation_at=EVALUATION_AT
        )
        self.fixture.session.merge(
            _authorized_routes(self.fixture.registry, failed=True)
        )
        after = self.fixture.adapter.read_snapshot(
            evaluation_at=EVALUATION_AT
        )
        self.assertEqual(before.observations, after.observations)
        self.assertEqual(before.store_revision, after.store_revision)
        self.assertEqual(before.evidence_revision, after.evidence_revision)
        self.assertNotEqual(before.outcome_revision, after.outcome_revision)
        self.assertNotEqual(before.snapshot_id, after.snapshot_id)

    def test_instance_method_shadows_are_rejected_without_callback(self) -> None:
        for name in ("read_snapshot", "_read_regular_bytes"):
            calls: list[str] = []

            def hostile(*args, **kwargs):
                del args, kwargs
                calls.append(PRIVATE_SENTINEL)
                raise RuntimeError(PRIVATE_SENTINEL)

            setattr(self.fixture.evidence_store, name, hostile)
            try:
                self.assertEqual(
                    "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                    _error_code(
                        lambda: self.fixture.adapter.read_snapshot(
                            evaluation_at=EVALUATION_AT
                        )
                    ),
                )
                self.assertEqual([], calls)
            finally:
                delattr(self.fixture.evidence_store, name)

        calls: list[str] = []

        def hostile_session_read(*args, **kwargs):
            del args, kwargs
            calls.append(PRIVATE_SENTINEL)
            raise RuntimeError(PRIVATE_SENTINEL)

        setattr(
            self.fixture.session,
            "_read_private_delivery_snapshot",
            hostile_session_read,
        )
        try:
            snapshot = self.fixture.adapter.read_snapshot(
                evaluation_at=EVALUATION_AT
            )
            self.assertEqual(3, len(snapshot.observations))
            self.assertEqual([], calls)
        finally:
            delattr(
                self.fixture.session,
                "_read_private_delivery_snapshot",
            )

    def test_lock_tamper_is_rejected_before_context_callback(self) -> None:
        calls: list[str] = []
        hostile = _HostileLock(calls)
        original_session_lock = self.fixture.session._lock
        original_adapter_lock = self.fixture.adapter._lock
        try:
            object.__setattr__(self.fixture.session, "_lock", hostile)
            object.__setattr__(self.fixture.adapter, "_lock", hostile)
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                _error_code(
                    lambda: self.fixture.adapter.read_snapshot(
                        evaluation_at=EVALUATION_AT
                    )
                ),
            )
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                _error_code(
                    lambda: EvidenceSession.private_delivery_source(
                        self.fixture.session
                    )
                ),
            )
            self.assertEqual([], calls)
        finally:
            object.__setattr__(
                self.fixture.session,
                "_lock",
                original_session_lock,
            )
            object.__setattr__(
                self.fixture.adapter,
                "_lock",
                original_adapter_lock,
            )

    def test_source_identity_is_bounded_before_path_or_seal_work(self) -> None:
        for name in ("trip_id", "slug"):
            original = getattr(self.fixture.evidence_store, name)
            try:
                setattr(
                    self.fixture.evidence_store,
                    name,
                    PRIVATE_SENTINEL * 100,
                )
                self.assertEqual(
                    "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                    _error_code(self.fixture.adapter.to_safe_dict),
                )
            finally:
                setattr(self.fixture.evidence_store, name, original)

    def test_instance_state_keys_and_counts_are_checked_before_lookup(
        self,
    ) -> None:
        cases = (
            (
                vars(self.fixture.evidence_store),
                "__init__",
                self.fixture.adapter.to_safe_dict,
            ),
            (
                vars(self.fixture.session),
                "_lock",
                self.fixture.adapter.to_safe_dict,
            ),
            (
                vars(self.fixture.session._clock),
                "_last",
                self.fixture.adapter.to_safe_dict,
            ),
        )
        for state, field_name, operation in cases:
            calls: list[str] = []
            original = state.pop(field_name, None)
            key = _HostileDictKey(calls, field_name)
            state[key] = original
            calls.clear()
            try:
                self.assertEqual(
                    "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                    _error_code(operation),
                )
                self.assertEqual([], calls)
            finally:
                del state[key]
                if original is not None:
                    state[field_name] = original

        source_state = vars(self.fixture.evidence_store)
        added = tuple(f"_synthetic_extra_{index}" for index in range(65))
        try:
            for name in added:
                source_state[name] = None
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                _error_code(self.fixture.adapter.to_safe_dict),
            )
        finally:
            for name in added:
                source_state.pop(name, None)

    def test_clock_tamper_is_rejected_before_hostile_callback(self) -> None:
        calls: list[str] = []
        original_session_clock = self.fixture.session._clock
        original_adapter_clock = self.fixture.adapter._clock
        hostile = _HostileClock(calls)
        try:
            object.__setattr__(self.fixture.session, "_clock", hostile)
            object.__setattr__(self.fixture.adapter, "_clock", hostile)
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                _error_code(self.fixture.adapter.to_safe_dict),
            )
            self.assertEqual([], calls)
        finally:
            object.__setattr__(
                self.fixture.session,
                "_clock",
                original_session_clock,
            )
            object.__setattr__(
                self.fixture.adapter,
                "_clock",
                original_adapter_clock,
            )

        def hostile_high_water() -> datetime:
            calls.append(PRIVATE_SENTINEL)
            raise RuntimeError(PRIVATE_SENTINEL)

        setattr(original_session_clock, "high_water", hostile_high_water)
        try:
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                _error_code(self.fixture.adapter.to_safe_dict),
            )
            self.assertEqual([], calls)
        finally:
            delattr(original_session_clock, "high_water")

    def test_observation_tamper_is_rejected_before_comparison(self) -> None:
        calls: list[str] = []
        observation = self.fixture.session._ledger.observations[0]
        original = observation.purge_at
        try:
            object.__setattr__(
                observation,
                "purge_at",
                _HostileComparable(calls),
            )
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                _error_code(
                    lambda: self.fixture.adapter.read_snapshot(
                        evaluation_at=EVALUATION_AT
                    )
                ),
            )
            self.assertEqual([], calls)
        finally:
            object.__setattr__(observation, "purge_at", original)

    def test_provider_problem_tamper_and_count_are_bounded_before_sort(
        self,
    ) -> None:
        self.fixture.session.merge(
            _authorized_routes(self.fixture.registry, failed=True)
        )
        problem = next(iter(self.fixture.session._problems.values()))
        calls: list[str] = []
        original = problem.code
        try:
            object.__setattr__(
                problem,
                "code",
                _HostileProblemCode(calls),
            )
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                _error_code(self.fixture.adapter.to_safe_dict),
            )
            self.assertEqual([], calls)
        finally:
            object.__setattr__(problem, "code", original)

        original_problems = self.fixture.session._problems
        try:
            object.__setattr__(
                self.fixture.session,
                "_problems",
                {str(index): problem for index in range(4097)},
            )
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                _error_code(self.fixture.adapter.to_safe_dict),
            )
        finally:
            object.__setattr__(
                self.fixture.session,
                "_problems",
                original_problems,
            )

    def test_policy_registry_tamper_is_rejected_before_property_access(
        self,
    ) -> None:
        calls: list[str] = []
        hostile = _HostilePolicies(calls)
        ledger = self.fixture.session._ledger
        original_ledger_policies = ledger.policies
        original_source_policies = self.fixture.evidence_store.policies
        try:
            object.__setattr__(ledger, "policies", hostile)
            self.fixture.evidence_store.policies = hostile
            self.assertEqual(
                "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                _error_code(self.fixture.adapter.to_safe_dict),
            )
            self.assertEqual([], calls)
        finally:
            object.__setattr__(ledger, "policies", original_ledger_policies)
            self.fixture.evidence_store.policies = original_source_policies

    def test_both_write_reviews_use_adapter_without_provider_or_write(
        self,
    ) -> None:
        writer_clock = _Clock(EVALUATION_AT)
        with mock.patch.object(
            EvidenceStore,
            "load",
            side_effect=AssertionError(PRIVATE_SENTINEL),
        ):
            for profile, leaf, expected, kwargs in (
                (
                    PrivateDeliveryProfile.HTML_PREVIEW,
                    "generation-preview",
                    {"index.html", "manifest.json"},
                    {},
                ),
                (
                    PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
                    "generation-ready",
                    {"calendar.ics", "index.html", "manifest.json"},
                    {"lodging_intake": self.fixture.lodging_intake},
                ),
            ):
                target = self.fixture.private_root / leaf
                review = prepare_private_delivery_write_review(
                    self.fixture.store,
                    self.fixture.adapter,
                    profile=profile,
                    private_root=self.fixture.private_root,
                    target_leaf=leaf,
                    clock=writer_clock,
                    **kwargs,
                )
                self.assertFalse(target.exists())
                self.assertEqual(
                    expected,
                    {item.filename for item in review.artifacts},
                )
                safe = review.to_safe_dict()
                self.assertFalse(safe["authorization_response_captured"])
                self.assertFalse(safe["writes_performed"])


if __name__ == "__main__":
    unittest.main()
