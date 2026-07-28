"""Offline tests for the run-scoped memory evidence boundary."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from trip_planner.evidence_session import (
    EvidenceSession,
    EvidenceSessionMerge,
)
from trip_planner.evidence_store import EvidenceStoreResult
from trip_planner.facts import (
    EvidenceLedger,
    EvidencePersistence,
    FactContractError,
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
from trip_planner.models import EvidenceState


UTC = timezone.utc
NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class MutableSource:
    def __init__(self, result: EvidenceStoreResult) -> None:
        self.result = result
        self.loads = 0

    def load(self) -> EvidenceStoreResult:
        self.loads += 1
        return self.result


def policies() -> ProviderPolicyRegistry:
    common = {
        "adapter_version": "v1",
        "contract_region": "test",
        "allowed_fact_kinds": (FactKind.ROUTE_ESTIMATE,),
        "allowed_value_fields": (
            "departure_at",
            "distance_km",
            "duration_min",
            "mode",
        ),
        "allowed_operations": ("compute-route",),
        "max_validity_seconds": 3600,
        "max_retention_seconds": 7200,
    }
    return ProviderPolicyRegistry(
        policies=(
            ProviderPolicy(
                policy_id="memory-route-v1",
                provider_id="memory-route",
                adapter_id="memory-route",
                persistence=EvidencePersistence.MEMORY_ONLY,
                **common,
            ),
            ProviderPolicy(
                policy_id="disk-route-v1",
                provider_id="disk-route",
                adapter_id="disk-route",
                persistence=EvidencePersistence.DISK_TTL,
                **common,
            ),
        )
    )


def base_result(
    registry: ProviderPolicyRegistry,
    *,
    revision: str = "a" * 64,
) -> EvidenceStoreResult:
    ledger = EvidenceLedger(registry)
    return EvidenceStoreResult(
        success=True,
        status="loaded",
        action="load",
        ledger=ledger,
        current_revision=revision,
        generation=0,
        purge_checked_at=NOW,
    )


def route_request(
    registry: ProviderPolicyRegistry,
    *,
    provider: str = "memory-route",
) -> ProviderRequest:
    policy = next(
        item for item in registry.policies if item.provider_id == provider
    )
    key = FactKey(
        kind=FactKind.ROUTE_ESTIMATE,
        subject_ids=("loc-a", "loc-b"),
        qualifiers=(
            ("mode", "walking"),
            ("departure_at", "2026-07-28T13:00:00Z"),
        ),
    )
    return ProviderRequest(
        provider_id=provider,
        adapter_id=provider,
        adapter_version="v1",
        operation="compute-route",
        fact_keys=(key,),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
    )


def route_result(
    request: ProviderRequest,
    *,
    status: ProviderResultStatus = ProviderResultStatus.SUCCESS,
    completed_at: datetime = NOW,
) -> ProviderResult:
    if status is ProviderResultStatus.FAILED:
        return ProviderResult(
            request_fingerprint=request.request_fingerprint,
            status=status,
            observations=(),
            problems=(
                ProviderProblem(
                    code=ProviderProblemCode.TIMEOUT,
                    message="The route provider timed out.",
                    retryable=True,
                    next_action="retry_with_budget",
                    fact_key_ids=(request.fact_keys[0].key_id,),
                ),
            ),
            attempts_used=1,
            completed_at=completed_at,
        )
    key = request.fact_keys[0]
    observation = FactObservation(
        key=key,
        value=FactValue.from_payload(
            FactKind.ROUTE_ESTIMATE,
            {
                "mode": "walking",
                "duration_min": 12.5,
                "distance_km": 0.9,
                "departure_at": "2026-07-28T13:00:00Z",
            },
        ),
        provenance=ProviderProvenance(
            provider_id=request.provider_id,
            adapter_id=request.adapter_id,
            adapter_version=request.adapter_version,
            request_fingerprint=request.request_fingerprint,
            retention_policy_id=request.policy_id,
            attributions=(),
        ),
        retrieved_at=completed_at,
        valid_until=completed_at + timedelta(minutes=30),
        purge_at=completed_at + timedelta(hours=1),
        confidence=1.0,
    )
    return ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=status,
        observations=(observation,),
        problems=(),
        attempts_used=1,
        completed_at=completed_at,
    )


class EvidenceSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = policies()
        self.clock = MutableClock(NOW)
        self.source = MutableSource(base_result(self.registry))
        self.session = EvidenceSession(
            self.source,
            clock=self.clock,
        )

    def authorize(
        self,
        request: ProviderRequest,
        result: ProviderResult,
    ):
        return authorize_provider_result(request, result, self.registry)

    def test_memory_result_composes_and_never_changes_store_revision(
        self,
    ) -> None:
        request = route_request(self.registry)
        merged = self.session.merge(
            self.authorize(request, route_result(request))
        )

        self.assertTrue(merged.changed)
        self.assertEqual("a" * 64, merged.current.store_revision)
        snapshot = merged.current.snapshot(evaluation_at=NOW)
        resolution = snapshot.resolve(request.fact_keys[0])
        self.assertEqual(EvidenceState.VERIFIED, resolution.evidence_state)
        self.assertEqual(12.5, resolution.selected.value.payload["duration_min"])
        self.assertNotEqual(
            EvidenceLedger(self.registry).revision,
            merged.current.ledger.revision,
        )
        self.assertGreaterEqual(self.source.loads, 2)

    def test_timeout_retains_lkg_and_exposes_exact_ephemeral_problem(
        self,
    ) -> None:
        request = route_request(self.registry)
        first = self.session.merge(
            self.authorize(request, route_result(request))
        )
        before = first.current.snapshot(evaluation_at=NOW)
        timed_out = self.session.merge(
            self.authorize(
                request,
                route_result(
                    request,
                    status=ProviderResultStatus.FAILED,
                ),
            )
        )

        self.assertFalse(timed_out.changed)
        self.assertEqual(
            first.current.ledger.revision,
            timed_out.current.ledger.revision,
        )
        self.assertEqual(
            ProviderProblemCode.TIMEOUT,
            timed_out.current.provider_problems[0].code,
        )
        self.assertEqual(
            (request.fact_keys[0].key_id,),
            timed_out.current.provider_problems[0].fact_key_ids,
        )
        after = timed_out.current.snapshot(evaluation_at=NOW)
        resolution = after.resolve(request.fact_keys[0])
        self.assertEqual(EvidenceState.VERIFIED, resolution.evidence_state)
        self.assertEqual(
            after.outcome_revision,
            after.to_dict()["outcome_revision"],
        )
        self.assertNotEqual(before.snapshot_id, after.snapshot_id)
        self.assertNotEqual(
            before.outcome_revision,
            after.outcome_revision,
        )

    def test_trusted_clock_clamps_rollback_and_prunes_retention(
        self,
    ) -> None:
        request = route_request(self.registry)
        self.session.merge(
            self.authorize(request, route_result(request))
        )
        self.clock.value = NOW - timedelta(days=1)
        rolled_back = self.session.load()
        self.assertEqual(NOW, rolled_back.purge_checked_at)
        self.assertEqual(1, len(rolled_back.ledger.observations))

        self.clock.value = NOW + timedelta(hours=2)
        expired = self.session.load()
        self.assertEqual(0, len(expired.ledger.observations))

    def test_session_rejects_disk_authorized_results(self) -> None:
        request = route_request(self.registry, provider="disk-route")
        authorized = self.authorize(request, route_result(request))

        with self.assertRaises(FactContractError) as caught:
            self.session.merge(authorized)
        self.assertEqual("UNTRUSTED_PROVENANCE", caught.exception.code)

    def test_durable_revision_drift_drops_endpoint_bound_memory(self) -> None:
        request = route_request(self.registry)
        self.session.merge(
            self.authorize(request, route_result(request))
        )
        self.source.result = base_result(
            self.registry,
            revision="b" * 64,
        )

        current = self.session.load()

        self.assertEqual("b" * 64, current.store_revision)
        self.assertEqual(0, len(current.ledger.observations))

    def test_same_key_retains_all_current_typed_problems(self) -> None:
        request = route_request(self.registry)
        failed = ProviderResult(
            request_fingerprint=request.request_fingerprint,
            status=ProviderResultStatus.FAILED,
            observations=(),
            problems=(
                ProviderProblem(
                    code=ProviderProblemCode.RATE_LIMITED,
                    message="The route provider rate limited this request.",
                    retryable=True,
                    next_action="retry_with_budget",
                    fact_key_ids=(request.fact_keys[0].key_id,),
                ),
                ProviderProblem(
                    code=ProviderProblemCode.TIMEOUT,
                    message="The route provider timed out.",
                    retryable=True,
                    next_action="retry_with_budget",
                    fact_key_ids=(request.fact_keys[0].key_id,),
                ),
            ),
            attempts_used=1,
            completed_at=NOW,
        )

        merged = self.session.merge(
            self.authorize(request, failed)
        )

        self.assertEqual(
            {
                ProviderProblemCode.RATE_LIMITED,
                ProviderProblemCode.TIMEOUT,
            },
            {item.code for item in merged.current.provider_problems},
        )

    def test_global_and_multikey_problem_identity_is_preserved(self) -> None:
        first = route_request(self.registry)
        second_key = FactKey(
            kind=FactKind.ROUTE_ESTIMATE,
            subject_ids=("loc-b", "loc-c"),
            qualifiers=(
                ("mode", "walking"),
                ("departure_at", "2026-07-28T14:00:00Z"),
            ),
        )
        request = ProviderRequest(
            provider_id=first.provider_id,
            adapter_id=first.adapter_id,
            adapter_version=first.adapter_version,
            operation=first.operation,
            fact_keys=(first.fact_keys[0], second_key),
            policy_id=first.policy_id,
            policy_digest=first.policy_digest,
        )
        original = (
            ProviderProblem(
                code=ProviderProblemCode.TIMEOUT,
                message="The route batch timed out.",
                retryable=True,
                next_action="retry_with_budget",
                fact_key_ids=(),
            ),
            ProviderProblem(
                code=ProviderProblemCode.RATE_LIMITED,
                message="The exact route pair was rate limited.",
                retryable=True,
                next_action="retry_with_budget",
                fact_key_ids=request.requested_key_ids,
            ),
        )
        result = ProviderResult(
            request_fingerprint=request.request_fingerprint,
            status=ProviderResultStatus.FAILED,
            observations=(),
            problems=original,
            attempts_used=1,
            completed_at=NOW,
        )

        merged = self.session.merge(
            self.authorize(request, result)
        )

        self.assertEqual(
            {
                item.code: item.to_dict()
                for item in result.problems
            },
            {
                item.code: item.to_dict()
                for item in merged.current.provider_problems
            },
        )
        self.assertEqual(
            (),
            next(
                item
                for item in merged.current.provider_problems
                if item.code is ProviderProblemCode.TIMEOUT
            ).fact_key_ids,
        )

        single_key = self.session.merge(
            self.authorize(
                first,
                route_result(
                    first,
                    status=ProviderResultStatus.FAILED,
                ),
            )
        )
        self.assertIn(
            original[0].to_dict(),
            [
                item.to_dict()
                for item in single_key.current.provider_problems
            ],
        )
        self.assertIn(
            original[1].to_dict(),
            [
                item.to_dict()
                for item in single_key.current.provider_problems
            ],
        )

        replacement = ProviderProblem(
            code=ProviderProblemCode.PROVIDER_UNAVAILABLE,
            message="The exact route batch is temporarily unavailable.",
            retryable=True,
            next_action="retry_with_budget",
            fact_key_ids=(),
        )
        replacement_result = ProviderResult(
            request_fingerprint=request.request_fingerprint,
            status=ProviderResultStatus.FAILED,
            observations=(),
            problems=(replacement,),
            attempts_used=1,
            completed_at=NOW,
        )
        replaced = self.session.merge(
            self.authorize(request, replacement_result)
        )
        self.assertEqual(
            [replacement.to_dict()],
            [
                item.to_dict()
                for item in replaced.current.provider_problems
            ],
        )

    def test_public_merge_wrapper_fails_closed_on_forged_diagnostics(
        self,
    ) -> None:
        current = self.session.load()
        with self.assertRaises(TypeError):
            EvidenceSessionMerge(current=current, changed="yes")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            EvidenceSessionMerge(
                current=current,
                changed=True,
                purged_observation_ids=("api_key=secret-value",),
            )


if __name__ == "__main__":
    unittest.main()
