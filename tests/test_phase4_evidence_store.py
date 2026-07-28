"""Offline adversarial tests for the Phase 4.1 evidence cache."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import trip_planner.evidence_store as evidence_store_module
import trip_planner.facts as facts_module
from trip_planner.evidence_store import (
    EvidenceStore,
    EvidenceStoreError,
)
from trip_planner.facts import (
    EvidencePersistence,
    FactKey,
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


UTC = timezone.utc
NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
DEPARTURE = "2026-10-03T01:00:00Z"
TRIP_ID = "trip-evidence-fixture"

ROUTE_FIELDS = (
    "arrival_at",
    "departure_at",
    "distance_km",
    "duration_min",
    "fallback_from_mode",
    "mode",
    "static_duration_min",
    "warning_codes",
)


def policy_registry(*, disk_retention_seconds: int = 3600) -> ProviderPolicyRegistry:
    return ProviderPolicyRegistry(
        policies=(
            ProviderPolicy(
                policy_id="disk-routes-v1",
                provider_id="disk-routes",
                adapter_id="disk-routes",
                adapter_version="v1",
                contract_region="test",
                allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
                allowed_value_fields=ROUTE_FIELDS,
                allowed_operations=("compute-route",),
                persistence=EvidencePersistence.DISK_TTL,
                max_validity_seconds=24 * 60 * 60,
                max_retention_seconds=disk_retention_seconds,
                required_attribution_labels=("Disk Routes",),
            ),
            ProviderPolicy(
                policy_id="memory-routes-v1",
                provider_id="memory-routes",
                adapter_id="memory-routes",
                adapter_version="v1",
                contract_region="test",
                allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
                allowed_value_fields=ROUTE_FIELDS,
                allowed_operations=("compute-route",),
                persistence=EvidencePersistence.MEMORY_ONLY,
                max_validity_seconds=24 * 60 * 60,
                max_retention_seconds=3600,
                required_attribution_labels=("Memory Routes",),
            ),
            ProviderPolicy(
                policy_id="google-place-id-v1",
                provider_id="google-places",
                adapter_id="google-places",
                adapter_version="v1",
                contract_region="test",
                allowed_fact_kinds=(FactKind.PLACE_IDENTITY,),
                allowed_value_fields=("provider_place_id",),
                allowed_operations=("resolve-place",),
                persistence=EvidencePersistence.INDEFINITE_ID,
                max_validity_seconds=366 * 24 * 60 * 60,
                max_retention_seconds=None,
                required_attribution_labels=("Google Maps",),
            ),
        )
    )


def route_key(
    origin: str = "loc-a",
    destination: str = "loc-b",
) -> FactKey:
    return FactKey(
        kind=FactKind.ROUTE_ESTIMATE,
        subject_ids=(origin, destination),
        qualifiers=(
            ("mode", "transit"),
            ("departure_at", DEPARTURE),
        ),
    )


def identity_key(location_id: str = "loc-place") -> FactKey:
    return FactKey(
        kind=FactKind.PLACE_IDENTITY,
        subject_ids=(location_id,),
        qualifiers=(("identity_provider", "google-places"),),
    )


def policy_for(
    policies: ProviderPolicyRegistry,
    provider_id: str,
) -> ProviderPolicy:
    return next(
        item for item in policies.policies
        if item.provider_id == provider_id
    )


def request_for(
    policies: ProviderPolicyRegistry,
    key: FactKey,
    *,
    provider_id: str = "disk-routes",
) -> ProviderRequest:
    policy = policy_for(policies, provider_id)
    return ProviderRequest(
        provider_id=provider_id,
        adapter_id=provider_id,
        adapter_version="v1",
        operation=(
            "resolve-place"
            if key.kind is FactKind.PLACE_IDENTITY
            else "compute-route"
        ),
        fact_keys=(key,),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
    )


def route_observation(
    policies: ProviderPolicyRegistry,
    key: FactKey,
    *,
    provider_id: str = "disk-routes",
    request: ProviderRequest | None = None,
    retrieved_at: datetime = NOW,
    valid_until: datetime | None = None,
    purge_at: datetime | None = None,
    duration_min: float = 30,
    source_suffix: str = "result",
) -> FactObservation:
    exact_request = request or request_for(
        policies, key, provider_id=provider_id
    )
    label = (
        "Disk Routes"
        if provider_id == "disk-routes"
        else "Memory Routes"
    )
    return FactObservation(
        key=key,
        value=FactValue.from_payload(
            FactKind.ROUTE_ESTIMATE,
            {
                "mode": "transit",
                "duration_min": duration_min,
                "distance_km": 8.5,
                "departure_at": DEPARTURE,
            },
        ),
        provenance=ProviderProvenance(
            provider_id=provider_id,
            adapter_id=provider_id,
            adapter_version="v1",
            request_fingerprint=exact_request.request_fingerprint,
            retention_policy_id=policy_for(
                policies, provider_id
            ).policy_id,
            response_id=f"response-{source_suffix}",
            source_uri=f"https://example.test/{source_suffix}",
            attributions=(
                (label, f"https://example.test/attr/{source_suffix}"),
            ),
        ),
        retrieved_at=retrieved_at,
        valid_until=valid_until or retrieved_at + timedelta(hours=2),
        purge_at=purge_at or retrieved_at + timedelta(hours=1),
        confidence=1,
    )


def identity_observation(
    policies: ProviderPolicyRegistry,
    *,
    key: FactKey | None = None,
    retrieved_at: datetime = NOW,
) -> FactObservation:
    exact_key = key or identity_key()
    request = request_for(
        policies, exact_key, provider_id="google-places"
    )
    return FactObservation(
        key=exact_key,
        value=FactValue.from_payload(
            FactKind.PLACE_IDENTITY,
            {"provider_place_id": "place-123"},
        ),
        provenance=ProviderProvenance(
            provider_id="google-places",
            adapter_id="google-places",
            adapter_version="v1",
            request_fingerprint=request.request_fingerprint,
            retention_policy_id="google-place-id-v1",
            attributions=(("Google Maps", None),),
        ),
        retrieved_at=retrieved_at,
        valid_until=retrieved_at + timedelta(days=365),
        purge_at=None,
        confidence=1,
    )


def authorized_success(
    policies: ProviderPolicyRegistry,
    observation: FactObservation,
) -> object:
    request = request_for(
        policies,
        observation.key,
        provider_id=observation.provenance.provider_id,
    )
    result = ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=ProviderResultStatus.SUCCESS,
        observations=(observation,),
        problems=(),
        attempts_used=1,
        completed_at=observation.retrieved_at,
    )
    return authorize_provider_result(request, result, policies)


def authorized_failure(
    policies: ProviderPolicyRegistry,
    key: FactKey,
    *,
    completed_at: datetime,
) -> object:
    request = request_for(policies, key)
    result = ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=ProviderResultStatus.FAILED,
        observations=(),
        problems=(
            ProviderProblem(
                code=ProviderProblemCode.TIMEOUT,
                message="Provider timed out without a usable response.",
                retryable=True,
                next_action="retry_provider",
                fact_key_ids=(key.key_id,),
            ),
        ),
        attempts_used=1,
        completed_at=completed_at,
    )
    return authorize_provider_result(request, result, policies)


def authorized_cache_hit(
    policies: ProviderPolicyRegistry,
    observation: FactObservation,
) -> object:
    request = request_for(
        policies,
        observation.key,
        provider_id=observation.provenance.provider_id,
    )
    result = ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=ProviderResultStatus.CACHE_HIT,
        observations=(observation,),
        problems=(),
        attempts_used=0,
        completed_at=observation.retrieved_at,
    )
    return authorize_provider_result(request, result, policies)


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.value


class DeterministicResetNonceSource:
    def __init__(self) -> None:
        self.values: list[bytes] = []

    @property
    def calls(self) -> int:
        return len(self.values)

    def __call__(self) -> bytes:
        nonce = (len(self.values) + 1).to_bytes(32, "big")
        self.values.append(nonce)
        return nonce


class InjectedFault(RuntimeError):
    pass


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, self._previous_umask)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.trips_root = self.root / "trips"
        self.slug = "evidence-fixture"
        self.data_dir = self.trips_root / self.slug / "data"
        self.data_dir.mkdir(parents=True)
        self.cache_path = (
            self.data_dir / ".trip-planner-evidence.json"
        )
        self.policies = policy_registry()
        self.clock = MutableClock()
        self.reset_nonces = DeterministicResetNonceSource()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def store(
        self,
        *,
        clock: MutableClock | None = None,
        fault_stage: str | None = None,
        policies: ProviderPolicyRegistry | None = None,
        reset_nonce_source=None,
    ) -> EvidenceStore:
        def fault_hook(stage: str) -> None:
            if stage == fault_stage:
                raise InjectedFault(stage)

        return EvidenceStore(
            self.trips_root,
            self.slug,
            TRIP_ID,
            policies or self.policies,
            clock=clock or self.clock,
            fault_hook=(
                fault_hook if fault_stage is not None else None
            ),
            reset_nonce_source=(
                self.reset_nonces
                if reset_nonce_source is None
                else reset_nonce_source
            ),
        )

    def assert_problem(self, result, code: str) -> None:
        self.assertIn(code, {item.code for item in result.problems})

    def expected_corrupt_epoch(self, payload: bytes) -> str:
        return evidence_store_module._corrupt_store_epoch(
            payload,
            self.reset_nonces.values[-1],
        )

    def expected_oversized_epoch(
        self,
        payload: bytes,
        size: int,
    ) -> str:
        return evidence_store_module._oversized_corrupt_store_epoch(
            payload,
            size,
            self.reset_nonces.values[-1],
        )

    def assert_empty_reset_document(
        self,
        *,
        policies: ProviderPolicyRegistry | None = None,
        expected_epoch: str | None = None,
    ) -> dict:
        document = json.loads(self.cache_path.read_text("utf-8"))
        self.assertEqual(
            evidence_store_module.EVIDENCE_STORE_VERSION,
            document["schema_version"],
        )
        self.assertEqual(TRIP_ID, document["trip_id"])
        self.assertEqual(
            (policies or self.policies).revision,
            document["policy_registry_revision"],
        )
        self.assertEqual(0, document["generation"])
        self.assertEqual([], document["records"])
        self.assertRegex(document["store_epoch"], r"^[0-9a-f]{64}$")
        if expected_epoch is not None:
            self.assertEqual(expected_epoch, document["store_epoch"])
        return document

    def test_missing_file_is_empty_and_one_clock_sample(self) -> None:
        result = self.store().load()

        self.assertTrue(result.success, result.to_dict())
        self.assertEqual("empty", result.status)
        self.assertEqual((), result.ledger.observations)
        self.assertFalse(self.cache_path.exists())
        self.assertEqual(1, self.clock.calls)
        self.assertEqual(64, len(result.current_revision))

    def test_disk_ttl_and_identity_round_trip_canonical_bytes(self) -> None:
        store = self.store()
        route = route_observation(self.policies, route_key())
        identity = identity_observation(self.policies)

        first = store.merge(authorized_success(self.policies, route))
        first_epoch = json.loads(
            self.cache_path.read_text("utf-8")
        )["store_epoch"]
        second = store.merge(
            authorized_success(self.policies, identity)
        )
        second_epoch = json.loads(
            self.cache_path.read_text("utf-8")
        )["store_epoch"]
        before = self.cache_path.read_bytes()
        loaded = self.store(clock=MutableClock()).load()

        self.assertTrue(first.success, first.to_dict())
        self.assertTrue(second.success, second.to_dict())
        self.assertTrue(loaded.success, loaded.to_dict())
        self.assertEqual({route, identity}, set(loaded.ledger.observations))
        self.assertEqual(second.current_revision, loaded.current_revision)
        self.assertEqual(first_epoch, second_epoch)
        self.assertRegex(first_epoch, r"^[0-9a-f]{64}$")
        self.assertEqual(before, self.cache_path.read_bytes())
        self.assertEqual(0o600, self.cache_path.stat().st_mode & 0o777)
        self.assertFalse(
            (self.data_dir / ".trip-planner-history").exists()
        )

    def test_success_result_builds_snapshot_with_exact_store_revision(
        self,
    ) -> None:
        item = route_observation(self.policies, route_key())
        merged = self.store().merge(
            authorized_success(self.policies, item)
        )

        snapshot = merged.snapshot(evaluation_at=NOW)

        self.assertEqual(merged.current_revision, snapshot.store_revision)
        self.assertNotEqual(
            merged.ledger.revision,
            snapshot.store_revision,
        )
        self.assertEqual((item,), snapshot.observations)

    def test_snapshot_future_evaluation_does_not_replace_purge_clock(
        self,
    ) -> None:
        key = route_key()
        item = route_observation(
            self.policies,
            key,
            valid_until=NOW + timedelta(hours=2),
            purge_at=NOW + timedelta(hours=1),
        )
        merged = self.store().merge(
            authorized_success(self.policies, item)
        )

        snapshot = merged.snapshot(
            evaluation_at=NOW + timedelta(hours=1)
        )
        resolution = snapshot.resolve(key)

        # evaluation_at controls semantic visibility; only the trusted store
        # clock may physically delete content or advance purge_checked_at.
        self.assertEqual(NOW, snapshot.purge_checked_at)
        self.assertEqual((item,), snapshot.observations)
        self.assertEqual("unverified", resolution.evidence_state.value)
        self.assertEqual("MISSING_EVIDENCE", resolution.reason.value)
        self.assertFalse(resolution.supports_travel_ready_use)

    def test_memory_only_rejected_before_lock_or_temp_creation(self) -> None:
        sentinel = "memory-only-sentinel-7b2d"
        item = route_observation(
            self.policies,
            route_key(),
            provider_id="memory-routes",
            source_suffix=sentinel,
        )
        result = self.store().merge(
            authorized_success(self.policies, item)
        )

        self.assertFalse(result.success)
        self.assert_problem(result, "MEMORY_ONLY_RESULT")
        self.assertFalse(self.cache_path.exists())
        self.assertFalse(
            (self.data_dir / ".trip-planner.lock").exists()
        )
        self.assertFalse(
            any(
                sentinel.encode("utf-8") in path.read_bytes()
                for path in self.data_dir.iterdir()
                if path.is_file()
            )
        )
        self.assertNotIn(sentinel, repr(result))
        self.assertNotIn(sentinel, repr(result.to_dict()))

    def test_purge_at_deadline_is_durable_and_prevents_aba(self) -> None:
        store = self.store()
        initial = store.load()
        item = route_observation(
            self.policies,
            route_key(),
            purge_at=NOW + timedelta(hours=1),
        )
        merged = store.merge(authorized_success(self.policies, item))
        self.clock.value = NOW + timedelta(hours=1)

        purged = store.load()
        self.clock.value = NOW
        after_clock_rollback = store.load()

        self.assertTrue(purged.success, purged.to_dict())
        self.assertEqual("purged", purged.status)
        self.assertEqual((), purged.ledger.observations)
        self.assertNotEqual(initial.current_revision, merged.current_revision)
        self.assertNotEqual(initial.current_revision, purged.current_revision)
        self.assertNotEqual(merged.current_revision, purged.current_revision)
        self.assertEqual(
            purged.current_revision,
            after_clock_rollback.current_revision,
        )
        self.assertEqual((), after_clock_rollback.ledger.observations)

    def test_stale_but_retained_record_is_not_deleted(self) -> None:
        item = route_observation(
            self.policies,
            route_key(),
            valid_until=NOW + timedelta(minutes=30),
            purge_at=NOW + timedelta(hours=1),
        )
        store = self.store()
        merged = store.merge(authorized_success(self.policies, item))
        self.clock.value = NOW + timedelta(minutes=30)

        loaded = store.load()

        self.assertTrue(loaded.success, loaded.to_dict())
        self.assertEqual((item,), loaded.ledger.observations)
        self.assertEqual(merged.current_revision, loaded.current_revision)
        self.assertFalse(loaded.changed)

    def test_failed_refresh_still_purges_expired_lkg(self) -> None:
        key = route_key()
        item = route_observation(
            self.policies,
            key,
            purge_at=NOW + timedelta(hours=1),
        )
        store = self.store()
        store.merge(authorized_success(self.policies, item))
        self.clock.value = NOW + timedelta(hours=2)

        failed = store.merge(
            authorized_failure(
                self.policies,
                key,
                completed_at=NOW + timedelta(hours=2),
            )
        )

        self.assertTrue(failed.success, failed.to_dict())
        self.assertTrue(failed.changed)
        self.assertEqual((), failed.ledger.observations)
        self.assertEqual(
            (item.observation_id,), failed.purged_observation_ids
        )
        self.assertIn(
            ProviderProblemCode.TIMEOUT,
            {problem.code for problem in failed.provider_problems},
        )

    def test_cleanup_is_idempotent(self) -> None:
        item = route_observation(
            self.policies,
            route_key(),
            purge_at=NOW + timedelta(hours=1),
        )
        store = self.store()
        store.merge(authorized_success(self.policies, item))
        before_epoch = json.loads(
            self.cache_path.read_text("utf-8")
        )["store_epoch"]
        self.clock.value = NOW + timedelta(hours=1)

        first = store.cleanup()
        after_first_epoch = json.loads(
            self.cache_path.read_text("utf-8")
        )["store_epoch"]
        second = store.cleanup()
        after_second_epoch = json.loads(
            self.cache_path.read_text("utf-8")
        )["store_epoch"]

        self.assertTrue(first.changed)
        self.assertEqual("cleaned", first.status)
        self.assertFalse(second.changed)
        self.assertEqual("no_op", second.status)
        self.assertEqual(first.current_revision, second.current_revision)
        self.assertEqual(
            before_epoch,
            after_first_epoch,
        )
        self.assertEqual(
            before_epoch,
            after_second_epoch,
        )

    def test_current_trip_corruption_is_cleared_without_salvage(self) -> None:
        corrupt_documents = (
            b"{",
            b"\xff",
            b'{"schema_version":"evidence-store/v2",'
            b'"schema_version":"evidence-store/v2"}',
            json.dumps(
                {
                    "schema_version": "evidence-store/v2",
                    "trip_id": TRIP_ID,
                    "policy_registry_revision": self.policies.revision,
                    "generation": True,
                    "records": [],
                    "store_epoch": "1" * 64,
                    "store_revision": "0" * 64,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )
        for index, payload in enumerate(corrupt_documents):
            with self.subTest(index=index):
                self.cache_path.write_bytes(payload)

                result = self.store(clock=MutableClock()).load()

                self.assertFalse(result.success)
                self.assert_problem(result, "CACHE_CORRUPTED")
                self.assertIsNone(result.ledger)
                self.assertTrue(result.changed)
                self.assert_empty_reset_document(
                    expected_epoch=self.expected_corrupt_epoch(payload)
                )
                reloaded = self.store(clock=MutableClock()).load()
                self.assertTrue(reloaded.success, reloaded.to_dict())
                self.assertEqual((), reloaded.ledger.observations)

    def test_malformed_cache_cannot_retain_expired_provider_bytes(
        self,
    ) -> None:
        sentinel = "malformed-expired-sentinel-5ea3"
        item = route_observation(
            self.policies,
            route_key(),
            source_suffix=sentinel,
            purge_at=NOW + timedelta(hours=1),
        )
        self.store().merge(authorized_success(self.policies, item))
        malformed = self.cache_path.read_bytes() + b"!"
        self.cache_path.write_bytes(malformed)
        self.clock.value = NOW + timedelta(hours=2)

        result = self.store().load()

        self.assertFalse(result.success)
        self.assert_problem(result, "CACHE_CORRUPTED")
        self.assertTrue(result.changed)
        self.assertNotIn(
            sentinel.encode("utf-8"),
            self.cache_path.read_bytes(),
        )
        self.assert_empty_reset_document(
            expected_epoch=self.expected_corrupt_epoch(malformed)
        )

    def test_corrupt_reset_epoch_prevents_empty_state_aba(self) -> None:
        first_payload = b"{"
        self.cache_path.write_bytes(first_payload)
        first = self.store(clock=MutableClock()).load()
        first_document = self.assert_empty_reset_document(
            expected_epoch=self.expected_corrupt_epoch(first_payload)
        )

        second_payload = first_payload
        self.cache_path.write_bytes(second_payload)
        second = self.store(clock=MutableClock()).load()
        second_document = self.assert_empty_reset_document(
            expected_epoch=self.expected_corrupt_epoch(second_payload)
        )

        self.assertTrue(first.changed)
        self.assertTrue(second.changed)
        self.assertEqual(0, first.generation)
        self.assertEqual(0, second.generation)
        self.assertNotEqual(
            first_document["store_epoch"],
            second_document["store_epoch"],
        )
        self.assertNotEqual(first.current_revision, second.current_revision)
        self.assertEqual(2, self.reset_nonces.calls)
        self.assertNotEqual(
            self.reset_nonces.values[0],
            self.reset_nonces.values[1],
        )

    def test_corrupt_reset_epoch_binds_domain_content_and_nonce(self) -> None:
        nonce_a = b"a" * 32
        nonce_b = b"b" * 32
        payload = b"same-corrupt-content"

        baseline = evidence_store_module._corrupt_store_epoch(
            payload,
            nonce_a,
        )

        self.assertNotEqual(
            baseline,
            evidence_store_module._corrupt_store_epoch(
                payload,
                nonce_b,
            ),
        )
        self.assertNotEqual(
            baseline,
            evidence_store_module._corrupt_store_epoch(
                payload + b"!",
                nonce_a,
            ),
        )
        self.assertNotEqual(
            baseline,
            evidence_store_module._oversized_corrupt_store_epoch(
                payload,
                len(payload),
                nonce_a,
            ),
        )

    def test_default_corrupt_reset_nonce_uses_256_bit_csprng(
        self,
    ) -> None:
        payload = b"{"
        nonce = b"\x9d" * 32
        self.cache_path.write_bytes(payload)
        store = EvidenceStore(
            self.trips_root,
            self.slug,
            TRIP_ID,
            self.policies,
            clock=MutableClock(),
        )

        with patch.object(
            evidence_store_module.secrets,
            "token_bytes",
            return_value=nonce,
        ) as entropy:
            result = store.load()

        self.assertFalse(result.success)
        self.assertTrue(result.changed)
        entropy.assert_called_once_with(32)
        self.assert_empty_reset_document(
            expected_epoch=evidence_store_module._corrupt_store_epoch(
                payload,
                nonce,
            )
        )

    def test_corrupt_reset_fails_closed_without_exact_nonce(self) -> None:
        payload = b"{"

        def unavailable_nonce() -> bytes:
            raise RuntimeError("injected entropy failure")

        for name, source in (
            ("unavailable", unavailable_nonce),
            ("short", lambda: b"x" * 31),
        ):
            with self.subTest(name=name):
                self.cache_path.write_bytes(payload)

                with self.assertRaises(EvidenceStoreError) as caught:
                    self.store(
                        clock=MutableClock(),
                        reset_nonce_source=source,
                    ).load()

                self.assertEqual(
                    "RESET_NONCE_UNAVAILABLE",
                    caught.exception.code,
                )
                self.assertEqual(payload, self.cache_path.read_bytes())

    def test_corrupt_reset_write_failure_preserves_original_bytes(
        self,
    ) -> None:
        payload = b"{"
        self.cache_path.write_bytes(payload)

        result = self.store(
            clock=MutableClock(),
            fault_stage="before_temp_write",
        ).load()

        self.assertFalse(result.success)
        self.assertEqual("write_failed", result.status)
        self.assert_problem(result, "CACHE_WRITE_FAILED")
        self.assertFalse(result.changed)
        self.assertEqual(payload, self.cache_path.read_bytes())

    def test_corrupt_cache_cannot_retain_expired_provider_bytes(
        self,
    ) -> None:
        sentinel = "corrupted-expired-sentinel-55e4"
        item = route_observation(
            self.policies,
            route_key(),
            source_suffix=sentinel,
            purge_at=NOW + timedelta(hours=1),
        )
        self.store().merge(authorized_success(self.policies, item))
        document = json.loads(self.cache_path.read_text("utf-8"))
        document["store_revision"] = "0" * 64
        self.cache_path.write_bytes(
            evidence_store_module._canonical_json_bytes(document)
        )
        self.clock.value = NOW + timedelta(hours=2)

        result = self.store().load()
        after = self.cache_path.read_bytes()
        reloaded = self.store(
            clock=MutableClock(NOW + timedelta(hours=2))
        ).load()

        self.assertFalse(result.success)
        self.assertEqual("corrupted", result.status)
        self.assert_problem(result, "CACHE_CORRUPTED")
        self.assertTrue(result.changed)
        self.assertNotIn(sentinel.encode("utf-8"), after)
        self.assertTrue(reloaded.success, reloaded.to_dict())
        self.assertEqual((), reloaded.ledger.observations)
        self.assertEqual(result.current_revision, reloaded.current_revision)

    def test_strict_document_metadata_and_canonical_form_are_enforced(
        self,
    ) -> None:
        item = route_observation(self.policies, route_key())
        self.store().merge(authorized_success(self.policies, item))
        valid_document = json.loads(self.cache_path.read_text("utf-8"))
        mutations = []
        wrong_revision = dict(valid_document)
        wrong_revision["store_revision"] = "0" * 64
        mutations.append(wrong_revision)
        wrong_schema = dict(valid_document)
        wrong_schema["schema_version"] = "evidence-store/v3"
        mutations.append(wrong_schema)
        extra_field = dict(valid_document)
        extra_field["unexpected"] = True
        mutations.append(extra_field)
        huge_generation = dict(valid_document)
        huge_generation["generation"] = 2**63
        mutations.append(huge_generation)

        payloads = [
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            for value in mutations
        ]
        payloads.append(
            json.dumps(
                valid_document,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
        )
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                self.cache_path.write_bytes(payload)
                result = self.store(clock=MutableClock()).load()
                self.assertFalse(result.success)
                self.assert_problem(result, "CACHE_CORRUPTED")
                self.assertTrue(result.changed)
                self.assert_empty_reset_document(
                    expected_epoch=self.expected_corrupt_epoch(payload)
                )

    def test_normalized_codec_has_one_canonical_byte_representation(
        self,
    ) -> None:
        route = route_observation(self.policies, route_key())
        identity = identity_observation(self.policies)
        store = self.store()
        store.merge(authorized_success(self.policies, route))
        store.merge(authorized_success(self.policies, identity))
        valid = json.loads(self.cache_path.read_text("utf-8"))

        reversed_records = json.loads(json.dumps(valid))
        reversed_records["records"].reverse()

        duplicate_attribution = json.loads(json.dumps(valid))
        attributions = duplicate_attribution["records"][0][
            "observation"
        ]["provenance"]["attributions"]
        attributions.append(dict(attributions[0]))

        alternate_number = json.loads(json.dumps(valid))
        route_record = next(
            record
            for record in alternate_number["records"]
            if record["observation"]["key"]["kind"] == "route_estimate"
        )
        route_record["observation"]["value"]["payload"][
            "duration_min"
        ] = 30

        alternate_timestamp = json.loads(json.dumps(valid))
        alternate_timestamp["records"][0]["observation"][
            "retrieved_at"
        ] = alternate_timestamp["records"][0]["observation"][
            "retrieved_at"
        ].replace("Z", "+00:00")

        arbitrary_separator = json.loads(json.dumps(valid))
        arbitrary_separator["records"][0]["observation"][
            "retrieved_at"
        ] = arbitrary_separator["records"][0]["observation"][
            "retrieved_at"
        ].replace("T", "Y", 1)

        for name, document in (
            ("record-order", reversed_records),
            ("duplicate-attribution", duplicate_attribution),
            ("alternate-number", alternate_number),
            ("alternate-timestamp", alternate_timestamp),
            ("arbitrary-separator", arbitrary_separator),
        ):
            with self.subTest(name=name):
                payload = json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.cache_path.write_bytes(payload)

                result = self.store(clock=MutableClock()).load()

                self.assertFalse(result.success)
                self.assert_problem(result, "CACHE_CORRUPTED")
                self.assertTrue(result.changed)
                self.assert_empty_reset_document(
                    expected_epoch=self.expected_corrupt_epoch(payload)
                )

    def test_policy_drift_clears_but_trip_file_transplant_is_preserved(
        self,
    ) -> None:
        item = route_observation(self.policies, route_key())
        self.store().merge(authorized_success(self.policies, item))
        before = self.cache_path.read_bytes()
        changed_policies = policy_registry(
            disk_retention_seconds=1800
        )

        transplanted = EvidenceStore(
            self.trips_root,
            self.slug,
            "trip-other",
            self.policies,
            clock=MutableClock(),
        ).load()
        after_transplant = self.cache_path.read_bytes()
        drift = self.store(
            clock=MutableClock(),
            policies=changed_policies,
        ).load()

        self.assertFalse(transplanted.success)
        self.assert_problem(transplanted, "CACHE_CORRUPTED")
        self.assertFalse(transplanted.changed)
        self.assertEqual(before, after_transplant)
        self.assertFalse(drift.success)
        self.assert_problem(drift, "CACHE_CORRUPTED")
        self.assertTrue(drift.changed)
        self.assertNotEqual(before, self.cache_path.read_bytes())
        self.assert_empty_reset_document(policies=changed_policies)

    def test_future_record_after_cross_process_clock_rollback_is_corrupt(
        self,
    ) -> None:
        future = NOW + timedelta(hours=2)
        future_clock = MutableClock(future)
        item = route_observation(
            self.policies,
            route_key(),
            retrieved_at=future,
            purge_at=future + timedelta(hours=1),
        )
        written = self.store(clock=future_clock).merge(
            authorized_success(self.policies, item)
        )
        before = self.cache_path.read_bytes()

        rolled_back = self.store(clock=MutableClock(NOW)).load()

        self.assertTrue(written.success, written.to_dict())
        self.assertFalse(rolled_back.success)
        self.assert_problem(rolled_back, "CACHE_CORRUPTED")
        self.assertEqual(before, self.cache_path.read_bytes())
        self.assertEqual(0, self.reset_nonces.calls)

    def test_mixed_expired_and_future_cache_is_cleared_in_full(
        self,
    ) -> None:
        expired = route_observation(
            self.policies,
            route_key("old-a", "old-b"),
            source_suffix="mixed-expired-sentinel",
            purge_at=NOW + timedelta(hours=1),
        )
        future = route_observation(
            self.policies,
            route_key("future-a", "future-b"),
            source_suffix="mixed-future-sentinel",
            retrieved_at=NOW + timedelta(hours=2),
            purge_at=NOW + timedelta(hours=3),
        )
        ledger = facts_module._restore_durable_evidence_ledger(
            policies=self.policies,
            observations=(expired, future),
            generation=2,
        )
        store_epoch = evidence_store_module._initial_store_epoch(
            TRIP_ID,
            self.policies,
        )
        store_revision = evidence_store_module._store_revision(
            TRIP_ID,
            self.policies,
            ledger,
            store_epoch,
        )
        payload = evidence_store_module._encode_document(
            trip_id=TRIP_ID,
            policies=self.policies,
            ledger=ledger,
            store_revision=store_revision,
            store_epoch=store_epoch,
        )
        self.cache_path.write_bytes(payload)
        os.chmod(self.cache_path, 0o600)

        result = self.store(
            clock=MutableClock(NOW + timedelta(hours=1, minutes=30))
        ).load()

        self.assertFalse(result.success)
        self.assert_problem(result, "CACHE_CORRUPTED")
        self.assertTrue(result.changed)
        self.assertEqual(
            tuple(
                sorted(
                    (expired.observation_id, future.observation_id)
                )
            ),
            result.purged_observation_ids,
        )
        self.assert_empty_reset_document(
            expected_epoch=self.expected_corrupt_epoch(payload)
        )

    def test_cache_and_lock_symlinks_fail_closed(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        self.cache_path.symlink_to(outside)
        with self.assertRaises(EvidenceStoreError):
            self.store()
        self.assertEqual(b"outside", outside.read_bytes())
        self.cache_path.unlink()

        store = self.store()
        lock_path = self.data_dir / ".trip-planner.lock"
        lock_path.symlink_to(outside)
        with self.assertRaises(EvidenceStoreError):
            store.load()
        self.assertEqual(b"outside", outside.read_bytes())

    def test_non_regular_cache_and_symlinked_data_directory_reject(
        self,
    ) -> None:
        fifo_path = self.cache_path
        os.mkfifo(fifo_path)
        with self.assertRaises(EvidenceStoreError):
            self.store()
        fifo_path.unlink()

        other_slug = "symlinked-data"
        other_trip = self.trips_root / other_slug
        other_trip.mkdir()
        outside_data = self.root / "outside-data"
        outside_data.mkdir()
        (other_trip / "data").symlink_to(
            outside_data, target_is_directory=True
        )
        with self.assertRaises(EvidenceStoreError):
            EvidenceStore(
                self.trips_root,
                other_slug,
                TRIP_ID,
                self.policies,
                clock=MutableClock(),
            )

    def test_pre_replace_faults_preserve_old_bytes_and_remove_temps(
        self,
    ) -> None:
        first = route_observation(self.policies, route_key())
        self.store().merge(authorized_success(self.policies, first))
        baseline = self.cache_path.read_bytes()
        second = route_observation(
            self.policies,
            route_key("loc-c", "loc-d"),
        )
        for stage in (
            "before_temp_write",
            "after_temp_write",
            "after_temp_fsync",
            "before_replace",
        ):
            with self.subTest(stage=stage):
                self.cache_path.write_bytes(baseline)
                result = self.store(
                    clock=MutableClock(),
                    fault_stage=stage,
                ).merge(authorized_success(self.policies, second))

                self.assertFalse(result.success)
                self.assertEqual("write_failed", result.status)
                self.assert_problem(result, "CACHE_WRITE_FAILED")
                self.assertEqual(baseline, self.cache_path.read_bytes())
                self.assertFalse(
                    any(
                        self.data_dir.glob(
                            ".trip-planner-evidence.*.tmp"
                        )
                    )
                )

    def test_pre_replace_temp_unlink_is_directory_fsynced(self) -> None:
        item = route_observation(self.policies, route_key())
        real_fsync = os.fsync
        fsync_kinds: list[str] = []

        def recording_fsync(descriptor: int) -> None:
            mode = os.fstat(descriptor).st_mode
            fsync_kinds.append(
                "directory" if stat.S_ISDIR(mode) else "file"
            )
            real_fsync(descriptor)

        with patch.object(
            evidence_store_module.os,
            "fsync",
            side_effect=recording_fsync,
        ):
            result = self.store(
                fault_stage="after_temp_fsync"
            ).merge(authorized_success(self.policies, item))

        self.assertFalse(result.success)
        self.assert_problem(result, "CACHE_WRITE_FAILED")
        self.assertEqual(["file", "directory"], fsync_kinds)
        self.assertFalse(
            any(
                self.data_dir.glob(
                    ".trip-planner-evidence.*.tmp"
                )
            )
        )

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_process_death_orphan_temp_is_removed_and_fsynced_on_load(
        self,
    ) -> None:
        sentinel = "crash-temp-sentinel-4a91"
        item = route_observation(
            self.policies,
            route_key(),
            purge_at=NOW + timedelta(minutes=1),
            source_suffix=sentinel,
        )
        authorized = authorized_success(self.policies, item)
        child = os.fork()
        if child == 0:
            def crash_after_fsync(stage: str) -> None:
                if stage == "after_temp_fsync":
                    os._exit(77)

            try:
                EvidenceStore(
                    self.trips_root,
                    self.slug,
                    TRIP_ID,
                    self.policies,
                    clock=MutableClock(),
                    fault_hook=crash_after_fsync,
                ).merge(authorized)
            except BaseException:
                os._exit(78)
            os._exit(79)

        waited, status = os.waitpid(child, 0)
        self.assertEqual(child, waited)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(77, os.WEXITSTATUS(status))
        orphan_paths = list(
            self.data_dir.glob(".trip-planner-evidence.*.tmp")
        )
        self.assertTrue(orphan_paths)
        self.assertTrue(
            any(
                sentinel.encode("utf-8") in path.read_bytes()
                for path in orphan_paths
            )
        )
        self.clock.value = NOW + timedelta(hours=1)

        loaded = self.store().load()

        self.assertTrue(loaded.success, loaded.to_dict())
        self.assertEqual("empty", loaded.status)
        self.assertFalse(
            any(
                self.data_dir.glob(
                    ".trip-planner-evidence.*.tmp"
                )
            )
        )

    def test_post_replace_fault_is_recovered_by_exact_retry(self) -> None:
        item = route_observation(self.policies, route_key())
        uncertain = self.store(
            fault_stage="after_replace"
        ).merge(authorized_success(self.policies, item))

        self.assertFalse(uncertain.success)
        self.assertEqual("outcome_unknown", uncertain.status)
        self.assert_problem(uncertain, "CACHE_OUTCOME_UNKNOWN")
        loaded = self.store(clock=MutableClock()).load()
        generation = loaded.ledger.generation
        replay = self.store(clock=MutableClock()).merge(
            authorized_success(self.policies, item)
        )

        self.assertTrue(loaded.success, loaded.to_dict())
        self.assertEqual((item,), loaded.ledger.observations)
        self.assertEqual(uncertain.expected_revision, loaded.current_revision)
        self.assertTrue(replay.success, replay.to_dict())
        self.assertEqual("no_op", replay.status)
        self.assertEqual(generation, replay.ledger.generation)

    def test_replace_lost_ack_is_outcome_unknown_and_reconcilable(
        self,
    ) -> None:
        item = route_observation(self.policies, route_key())
        real_replace = os.replace

        def replace_then_lose_ack(source, destination) -> None:
            real_replace(source, destination)
            raise OSError("simulated lost rename acknowledgement")

        with patch.object(
            evidence_store_module.os,
            "replace",
            side_effect=replace_then_lose_ack,
        ):
            uncertain = self.store().merge(
                authorized_success(self.policies, item)
            )

        loaded = self.store(clock=MutableClock()).load()

        self.assertFalse(uncertain.success)
        self.assertEqual("outcome_unknown", uncertain.status)
        self.assert_problem(uncertain, "CACHE_OUTCOME_UNKNOWN")
        self.assertTrue(uncertain.changed)
        self.assertTrue(loaded.success, loaded.to_dict())
        self.assertEqual(uncertain.expected_revision, loaded.current_revision)
        self.assertEqual((item,), loaded.ledger.observations)

    def test_after_directory_fsync_is_outcome_unknown_but_readable(
        self,
    ) -> None:
        item = route_observation(self.policies, route_key())
        uncertain = self.store(
            fault_stage="after_directory_fsync"
        ).merge(authorized_success(self.policies, item))

        self.assertFalse(uncertain.success)
        self.assert_problem(uncertain, "CACHE_OUTCOME_UNKNOWN")
        loaded = self.store(clock=MutableClock()).load()
        self.assertTrue(loaded.success, loaded.to_dict())
        self.assertEqual((item,), loaded.ledger.observations)
        self.assertEqual(uncertain.expected_revision, loaded.current_revision)

    def test_exact_duplicate_is_idempotent_without_generation_change(
        self,
    ) -> None:
        item = route_observation(self.policies, route_key())
        store = self.store()
        first = store.merge(authorized_success(self.policies, item))
        second = store.merge(authorized_success(self.policies, item))

        self.assertTrue(first.success, first.to_dict())
        self.assertTrue(second.success, second.to_dict())
        self.assertEqual("no_op", second.status)
        self.assertTrue(second.replayed)
        self.assertEqual(first.generation, second.generation)
        self.assertEqual(first.current_revision, second.current_revision)

        cache_hit = store.merge(
            authorized_cache_hit(self.policies, item)
        )
        self.assertTrue(cache_hit.success, cache_hit.to_dict())
        self.assertEqual("no_op", cache_hit.status)
        self.assertEqual(first.generation, cache_hit.generation)
        self.assertEqual(first.current_revision, cache_hit.current_revision)

    def test_cache_hit_mismatch_cannot_prevent_expiry_purge(self) -> None:
        key = route_key()
        current = route_observation(
            self.policies,
            key,
            purge_at=NOW + timedelta(hours=1),
        )
        store = self.store()
        store.merge(authorized_success(self.policies, current))
        replacement = route_observation(
            self.policies,
            key,
            retrieved_at=NOW + timedelta(hours=1),
            purge_at=NOW + timedelta(hours=2),
            duration_min=31,
            source_suffix="mismatched-cache-hit",
        )
        self.clock.value = NOW + timedelta(hours=1)

        rejected = store.merge(
            authorized_cache_hit(self.policies, replacement)
        )
        loaded = store.load()

        self.assertFalse(rejected.success)
        self.assert_problem(rejected, "INVALID_PROVIDER_RESPONSE")
        self.assertTrue(rejected.changed)
        self.assertEqual((current.observation_id,), rejected.purged_observation_ids)
        self.assertEqual((), rejected.ledger.observations)
        self.assertEqual((), loaded.ledger.observations)
        self.assertEqual(rejected.current_revision, loaded.current_revision)

    def test_concurrent_different_slots_do_not_lose_updates(self) -> None:
        first = route_observation(self.policies, route_key())
        second = route_observation(
            self.policies,
            route_key("loc-c", "loc-d"),
        )
        barrier = threading.Barrier(2)

        def merge(item: FactObservation):
            barrier.wait(timeout=5)
            return self.store(clock=MutableClock()).merge(
                authorized_success(self.policies, item)
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                future.result(timeout=15)
                for future in (
                    executor.submit(merge, first),
                    executor.submit(merge, second),
                )
            ]
        loaded = self.store(clock=MutableClock()).load()

        self.assertTrue(all(result.success for result in results))
        self.assertEqual({first, second}, set(loaded.ledger.observations))

    def test_older_same_slot_cannot_overwrite_newer_lkg(self) -> None:
        key = route_key()
        newer = route_observation(
            self.policies,
            key,
            retrieved_at=NOW + timedelta(minutes=10),
            purge_at=NOW + timedelta(hours=1),
            duration_min=20,
            source_suffix="newer",
        )
        older = route_observation(
            self.policies,
            key,
            duration_min=40,
            source_suffix="older",
        )
        clock = MutableClock(NOW + timedelta(minutes=10))
        store = self.store(clock=clock)
        first = store.merge(authorized_success(self.policies, newer))
        second = store.merge(authorized_success(self.policies, older))

        self.assertTrue(first.success, first.to_dict())
        self.assertTrue(second.success, second.to_dict())
        self.assertEqual("no_op", second.status)
        self.assertFalse(second.replayed)
        self.assertEqual((newer,), second.ledger.observations)
        self.assertEqual(first.current_revision, second.current_revision)

    def test_record_capacity_write_failure_preserves_readable_lkg(
        self,
    ) -> None:
        first = route_observation(self.policies, route_key())
        second = route_observation(
            self.policies,
            route_key("loc-c", "loc-d"),
        )
        store = self.store()
        accepted = store.merge(
            authorized_success(self.policies, first)
        )
        baseline = self.cache_path.read_bytes()

        with patch.object(evidence_store_module, "_MAX_RECORDS", 1):
            rejected = store.merge(
                authorized_success(self.policies, second)
            )
            loaded = self.store(clock=MutableClock()).load()

        self.assertTrue(accepted.success, accepted.to_dict())
        self.assertFalse(rejected.success)
        self.assertEqual("write_failed", rejected.status)
        self.assert_problem(rejected, "CACHE_WRITE_FAILED")
        self.assertFalse(rejected.changed)
        self.assertEqual(baseline, self.cache_path.read_bytes())
        self.assertTrue(loaded.success, loaded.to_dict())
        self.assertEqual((first,), loaded.ledger.observations)

    def test_capacity_failure_still_durably_purges_expired_lkg(
        self,
    ) -> None:
        policies = policy_registry(disk_retention_seconds=3 * 60 * 60)
        store = self.store(policies=policies)
        sentinel = "expired-capacity-sentinel-7d4c"
        expired = route_observation(
            policies,
            route_key("old-a", "old-b"),
            purge_at=NOW + timedelta(hours=1),
            source_suffix=sentinel,
        )
        retained = route_observation(
            policies,
            route_key("keep-a", "keep-b"),
            purge_at=NOW + timedelta(hours=2),
            source_suffix="retained",
        )
        store.merge(authorized_success(policies, expired))
        store.merge(authorized_success(policies, retained))

        keys = (
            route_key("new-a", "new-b"),
            route_key("new-c", "new-d"),
        )
        policy = policy_for(policies, "disk-routes")
        request = ProviderRequest(
            provider_id="disk-routes",
            adapter_id="disk-routes",
            adapter_version="v1",
            operation="compute-route",
            fact_keys=keys,
            policy_id=policy.policy_id,
            policy_digest=policy.policy_digest,
        )
        promoted = tuple(
            route_observation(
                policies,
                key,
                request=request,
                retrieved_at=NOW + timedelta(hours=1),
                purge_at=NOW + timedelta(hours=3),
                source_suffix=f"new-{index}",
            )
            for index, key in enumerate(keys)
        )
        raw = ProviderResult(
            request_fingerprint=request.request_fingerprint,
            status=ProviderResultStatus.SUCCESS,
            observations=promoted,
            problems=(),
            attempts_used=1,
            completed_at=NOW + timedelta(hours=1),
        )
        authorized = authorize_provider_result(request, raw, policies)
        self.clock.value = NOW + timedelta(hours=1)

        with patch.object(evidence_store_module, "_MAX_RECORDS", 2):
            rejected = store.merge(authorized)
            after = self.cache_path.read_bytes()
            loaded = self.store(
                clock=MutableClock(NOW + timedelta(hours=1)),
                policies=policies,
            ).load()

        self.assertFalse(rejected.success)
        self.assertEqual("write_failed", rejected.status)
        self.assert_problem(rejected, "CACHE_WRITE_FAILED")
        self.assertTrue(rejected.changed)
        self.assertEqual(
            (expired.observation_id,),
            rejected.purged_observation_ids,
        )
        self.assertNotIn(sentinel.encode("utf-8"), after)
        self.assertTrue(loaded.success, loaded.to_dict())
        self.assertEqual((retained,), loaded.ledger.observations)
        self.assertEqual(rejected.current_revision, loaded.current_revision)

    def test_encoded_size_failure_is_typed_and_writes_no_cache(self) -> None:
        item = route_observation(self.policies, route_key())

        with patch.object(
            evidence_store_module,
            "_MAX_CACHE_BYTES",
            256,
        ):
            rejected = self.store().merge(
                authorized_success(self.policies, item)
            )

        self.assertFalse(rejected.success)
        self.assertEqual("write_failed", rejected.status)
        self.assert_problem(rejected, "CACHE_WRITE_FAILED")
        self.assertFalse(rejected.changed)
        self.assertFalse(self.cache_path.exists())

    def test_lstat_oversize_race_uses_small_opened_cache(self) -> None:
        item = route_observation(self.policies, route_key())
        store = self.store()
        merged = store.merge(authorized_success(self.policies, item))
        self.assertTrue(merged.success, merged.to_dict())
        before = self.cache_path.read_bytes()
        self.assertLess(len(before), evidence_store_module._MAX_CACHE_BYTES)
        real_lstat = Path.lstat

        def inflated_lstat(path: Path) -> os.stat_result:
            info = real_lstat(path)
            if path == self.cache_path:
                values = list(info)
                values[6] = evidence_store_module._MAX_CACHE_BYTES + 1
                return os.stat_result(values)
            return info

        with patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=inflated_lstat,
        ):
            loaded = store.load()

        self.assertTrue(loaded.success, loaded.to_dict())
        self.assertEqual("loaded", loaded.status)
        self.assertEqual((item,), loaded.ledger.observations)
        self.assertEqual(before, self.cache_path.read_bytes())
        self.assertEqual(0, self.reset_nonces.calls)

    def test_oversized_cache_with_unknown_owner_is_preserved(
        self,
    ) -> None:
        payload = b"x" * 1025
        self.cache_path.write_bytes(payload)
        os.chmod(self.cache_path, 0o600)

        with patch.object(
            evidence_store_module,
            "_MAX_CACHE_BYTES",
            1024,
        ):
            result = self.store(clock=MutableClock()).load()

        self.assertFalse(result.success)
        self.assert_problem(result, "CACHE_CORRUPTED")
        self.assertFalse(result.changed)
        self.assertEqual(payload, self.cache_path.read_bytes())
        self.assertEqual(0, self.reset_nonces.calls)

    def test_oversized_current_trip_envelope_is_cleared(self) -> None:
        item = route_observation(self.policies, route_key())
        self.store().merge(authorized_success(self.policies, item))
        payload = self.cache_path.read_bytes()
        self.assertGreater(len(payload), 1024)

        with patch.object(
            evidence_store_module,
            "_MAX_CACHE_BYTES",
            1024,
        ):
            result = self.store(clock=MutableClock()).load()

        self.assertFalse(result.success)
        self.assert_problem(result, "CACHE_CORRUPTED")
        self.assertTrue(result.changed)
        self.assert_empty_reset_document(
            expected_epoch=self.expected_oversized_epoch(
                payload[:1025], len(payload)
            )
        )

    def test_oversized_foreign_trip_envelope_is_preserved(self) -> None:
        item = route_observation(self.policies, route_key())
        self.store().merge(authorized_success(self.policies, item))
        payload = self.cache_path.read_bytes().replace(
            TRIP_ID.encode("utf-8"), b"trip-other"
        )
        self.assertGreater(len(payload), 1024)
        self.cache_path.write_bytes(payload)
        os.chmod(self.cache_path, 0o600)

        with patch.object(
            evidence_store_module,
            "_MAX_CACHE_BYTES",
            1024,
        ):
            result = self.store(clock=MutableClock()).load()

        self.assertFalse(result.success)
        self.assert_problem(result, "CACHE_CORRUPTED")
        self.assertFalse(result.changed)
        self.assertEqual(payload, self.cache_path.read_bytes())
        self.assertEqual(0, self.reset_nonces.calls)

    def test_oversized_permissive_cache_mode_fails_before_probe(self) -> None:
        payload = b"x" * 1025
        self.cache_path.write_bytes(payload)
        os.chmod(self.cache_path, 0o644)

        with patch.object(
            evidence_store_module,
            "_MAX_CACHE_BYTES",
            1024,
        ):
            with self.assertRaises(EvidenceStoreError) as caught:
                self.store(clock=MutableClock()).load()

        self.assertEqual("UNSAFE_EVIDENCE_PERMISSIONS", caught.exception.code)
        self.assertEqual(payload, self.cache_path.read_bytes())
        self.assertEqual(0, self.reset_nonces.calls)

    def test_permissive_valid_cache_mode_fails_closed(self) -> None:
        item = route_observation(self.policies, route_key())
        self.store().merge(authorized_success(self.policies, item))
        os.chmod(self.cache_path, 0o644)

        with self.assertRaises(EvidenceStoreError) as caught:
            self.store(clock=MutableClock()).load()

        self.assertEqual("UNSAFE_EVIDENCE_PERMISSIONS", caught.exception.code)
        self.assertEqual(0o644, stat.S_IMODE(self.cache_path.stat().st_mode))

    def test_valid_cache_owned_by_another_user_fails_closed(self) -> None:
        item = route_observation(self.policies, route_key())
        self.store().merge(authorized_success(self.policies, item))

        with patch.object(
            evidence_store_module.os,
            "geteuid",
            return_value=os.geteuid() + 1,
        ):
            with self.assertRaises(EvidenceStoreError) as caught:
                self.store(clock=MutableClock()).load()

        self.assertEqual("UNSAFE_EVIDENCE_PERMISSIONS", caught.exception.code)

    def test_saturated_generation_still_performs_irreversible_purge(
        self,
    ) -> None:
        item = route_observation(
            self.policies,
            route_key(),
            purge_at=NOW + timedelta(hours=1),
        )
        self.store().merge(authorized_success(self.policies, item))
        document = json.loads(self.cache_path.read_text("utf-8"))
        saturated = facts_module._restore_durable_evidence_ledger(
            policies=self.policies,
            observations=(item,),
            generation=evidence_store_module._MAX_GENERATION,
        )
        document["generation"] = saturated.generation
        document["store_revision"] = (
            evidence_store_module._store_revision(
                TRIP_ID,
                self.policies,
                saturated,
                document["store_epoch"],
            )
        )
        self.cache_path.write_bytes(
            evidence_store_module._canonical_json_bytes(document)
        )
        self.clock.value = NOW + timedelta(hours=1)

        purged = self.store().load()
        reloaded = self.store(clock=MutableClock(
            NOW + timedelta(hours=1)
        )).load()

        self.assertTrue(purged.success, purged.to_dict())
        self.assertEqual("purged", purged.status)
        self.assertEqual((), purged.ledger.observations)
        self.assertEqual(
            evidence_store_module._MAX_GENERATION,
            purged.generation,
        )
        self.assertTrue(reloaded.success, reloaded.to_dict())
        self.assertEqual(purged.current_revision, reloaded.current_revision)

    def test_naive_trusted_clock_is_typed_and_lock_recovers(self) -> None:
        naive_clock = MutableClock(NOW.replace(tzinfo=None))
        store = self.store(clock=naive_clock)

        with self.assertRaises(EvidenceStoreError) as caught:
            store.load()
        self.assertEqual("INVALID_TRUSTED_CLOCK", caught.exception.code)

        recovered = self.store(clock=MutableClock()).load()
        self.assertTrue(recovered.success, recovered.to_dict())


if __name__ == "__main__":
    unittest.main()
