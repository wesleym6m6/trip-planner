"""Synthetic contracts for Phase 6.3A canonical lodging readiness."""

from __future__ import annotations

import copy
import json
import pickle
import sys
import unittest
from collections.abc import Iterator, Mapping
from datetime import timedelta
from unittest.mock import patch

import trip_planner
import trip_planner.facts as facts_module
import trip_planner.places_identity as places_identity_module
from trip_planner import compose_trip_state
from trip_planner.codec import build_plan, encode_plan
from trip_planner.evidence_integrity import validate_evidence_snapshot_integrity
from trip_planner.facts import (
    EvidenceLedger,
    EvidencePersistence,
    EvidenceSnapshot,
    EvidenceState,
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderPolicy,
    ProviderPolicyRegistry,
    ProviderProvenance,
)
from trip_planner.places_identity import extract_fresh_google_place_endpoint
from trip_planner.readiness import (
    CANONICAL_LODGING_EVIDENCE_VERSION,
    CanonicalLodgingEvidenceAssessment,
    assess_canonical_lodging_evidence,
    assess_trip_readiness,
)
from trip_planner.private_delivery import (
    PRIVATE_DELIVERY_MANIFEST_VERSION,
    PrivateDeliveryError,
    PrivateDeliveryProfile,
    prepare_private_delivery_review,
)
from tests.test_phase45_lodging_evidence import (
    NOW,
    _identity_observation,
    _policies as _identity_policies,
    _snapshot as _identity_snapshot,
)
from tests.test_phase46_readiness import _empty_snapshot, _lodging_plan
from tests.test_phase44_e2e import _plan as _single_day_plan
from tests.test_phase4_fact_contracts import (
    POLICIES as ALL_FACT_POLICIES,
    google_identity_observation,
    google_identity_request,
    observation as route_observation,
    opening_key,
    opening_value,
    place_identity_key,
    place_profile_key,
    provenance as fact_provenance,
)


LODGING_PROBLEM = "LODGING_EVIDENCE_UNVERIFIED"
PRIVATE_PLACE_ID = "ChIJ-phase63a-private-place-sentinel"
SOURCE_PATH = "/synthetic/phase63a/plan.json"
TARGET_PATH = "/synthetic/phase63a/private-output"


class _OneShotPlanMapping(Mapping):
    """Expose one detached view and reject every later source read."""

    def __init__(self, plan) -> None:
        self._items = tuple(plan.items())
        self.items_calls = 0
        self.unexpected_reads = 0

    def items(self):
        self.items_calls += 1
        if self.items_calls != 1:
            raise AssertionError("canonical mapping was read twice")
        return self._items

    def __getitem__(self, key):
        del key
        self.unexpected_reads += 1
        raise AssertionError("canonical mapping was read after detachment")

    def __iter__(self) -> Iterator:
        self.unexpected_reads += 1
        raise AssertionError("canonical mapping was iterated after detachment")

    def __len__(self) -> int:
        self.unexpected_reads += 1
        raise AssertionError("canonical mapping length was read after detachment")


class _EvilKey(str):
    """Become hostile only during canonical lodging key lookup."""


    def __new__(cls, value: str, hostile_calls: list[str]):
        instance = str.__new__(cls, value)
        instance._hostile_calls = hostile_calls
        return instance

    @staticmethod
    def _in_canonical_lodging_location_ids() -> bool:
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_name == "_canonical_lodging_location_ids":
                return True
            frame = frame.f_back
        return False

    def _fail_during_lodging_lookup(self, operation: str) -> None:
        if self._in_canonical_lodging_location_ids():
            self._hostile_calls.append(operation)
            raise AssertionError("hostile canonical lodging key was invoked")

    def __eq__(self, other):
        self._fail_during_lodging_lookup("equality")
        return str.__eq__(self, other)

    def __hash__(self):
        self._fail_during_lodging_lookup("hash")
        return str.__hash__(self)


class _EvilText(str):
    """An otherwise valid text subclass rejected at the batch boundary."""


def _location_ids(plan) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item["location_id"]
            for item in plan["state"]["trip"]["lodgings"]
        )
    )


def _identity_observations(
    plan,
    *,
    first_valid_for: timedelta = timedelta(days=2),
) -> tuple[FactObservation, ...]:
    policies = _identity_policies()
    return tuple(
        _identity_observation(
            policies,
            location_id=location_id,
            place_id=f"{PRIVATE_PLACE_ID}-{index}",
            valid_until=NOW + first_valid_for + timedelta(days=index),
        )
        for index, location_id in enumerate(_location_ids(plan))
    )


def _reseal_snapshot(
    policies: ProviderPolicyRegistry,
    observations: tuple[FactObservation, ...],
    *,
    evaluation_at,
    purge_checked_at,
) -> EvidenceSnapshot:
    """Build aggregate seals after deliberate post-factory nested mutation."""

    ordered = tuple(
        sorted(
            observations,
            key=lambda item: (
                item.key.key_id,
                item.provenance.provider_id,
                item.observation_id,
            ),
        )
    )
    evidence_revision = facts_module._digest(
        {"observation_ids": [item.observation_id for item in ordered]},
        prefix="active-evidence",
    )
    store_revision = "e" * 64
    snapshot_payload = {
        "contract_version": "evidence-snapshot/v1",
        "policy_registry_revision": policies.revision,
        "store_revision": store_revision,
        "evidence_revision": evidence_revision,
        "evaluation_at": evaluation_at.isoformat().replace("+00:00", "Z"),
        "purge_checked_at": purge_checked_at.isoformat().replace(
            "+00:00",
            "Z",
        ),
    }
    return EvidenceSnapshot(
        policies=policies,
        observations=ordered,
        evaluation_at=evaluation_at,
        purge_checked_at=purge_checked_at,
        store_revision=store_revision,
        evidence_revision=evidence_revision,
        snapshot_id=facts_module._digest(
            snapshot_payload,
            prefix="evidence-snapshot",
        ),
        _token=facts_module._SNAPSHOT_TOKEN,
    )


def _reseal_observation(observation: FactObservation) -> None:
    object.__setattr__(
        observation,
        "observation_id",
        facts_module._digest(
            observation._identity_payload(),
            prefix="fact-observation",
        ),
    )


def _assessment(plan, snapshot):
    composed = compose_trip_state(plan, snapshot)
    return composed, assess_canonical_lodging_evidence(
        canonical_plan=plan,
        composed=composed,
        snapshot=snapshot,
    )


def _readiness(plan, snapshot):
    composed = compose_trip_state(plan, snapshot)
    return assess_trip_readiness(
        canonical_plan=plan,
        composed=composed,
        snapshot=snapshot,
    )


def _duplicate_location_split_plan():
    original = _lodging_plan(split=True)
    state = copy.deepcopy(original["state"])
    location_id = state["trip"]["lodgings"][0]["location_id"]
    state["trip"]["lodgings"][1]["location_id"] = location_id
    days = state["itinerary"]["days"]
    days[1]["end_location_id"] = location_id
    days[2]["start_location_id"] = location_id
    days[2]["end_location_id"] = location_id
    return build_plan(
        trip_id=original["trip_id"],
        generation=original["generation"],
        state=state,
    )


def _custom_policy_snapshot(
    location_id: str,
) -> tuple[EvidenceSnapshot, FactObservation]:
    policy = ProviderPolicy(
        policy_id="google-place-id-v1",
        provider_id="google-places",
        adapter_id="google-places",
        adapter_version="v1",
        contract_region="test",
        allowed_fact_kinds=(FactKind.PLACE_IDENTITY,),
        allowed_value_fields=("provider_place_id",),
        allowed_operations=("resolve-place",),
        persistence=EvidencePersistence.INDEFINITE_ID,
        max_validity_seconds=365 * 24 * 60 * 60,
        max_retention_seconds=None,
        required_attribution_labels=("Google Maps",),
    )
    policies = ProviderPolicyRegistry((policy,))
    key = FactKey(
        kind=FactKind.PLACE_IDENTITY,
        subject_ids=(location_id,),
        qualifiers=(("identity_provider", "google-places"),),
    )
    observation = FactObservation(
        key=key,
        value=FactValue.from_payload(
            FactKind.PLACE_IDENTITY,
            {"provider_place_id": PRIVATE_PLACE_ID},
        ),
        provenance=ProviderProvenance(
            provider_id="google-places",
            adapter_id="google-places",
            adapter_version="v1",
            request_fingerprint="c" * 64,
            retention_policy_id=policy.policy_id,
            provider_record_id=PRIVATE_PLACE_ID,
            attributions=(("Google Maps", None),),
        ),
        retrieved_at=NOW - timedelta(minutes=5),
        valid_until=NOW + timedelta(days=2),
        purge_at=None,
        confidence=1.0,
    )
    return (
        _identity_snapshot(observation, policies=policies),
        observation,
    )


def _fully_ready_lodging_plan(*, split: bool = False):
    if split:
        plan = _lodging_plan(split=True)
        state = copy.deepcopy(plan["state"])
        first_location = state["trip"]["lodgings"][0]["location_id"]
        state["itinerary"]["days"][1]["places"] = []
        state["itinerary"]["days"][0]["places"] = [
            {
                "activity_id": "split-fixture-event",
                "title": "split fixture event",
                "location_id": first_location,
                "time": "16:00",
                "duration_min": 30,
                "decision_state": "booked",
                "flexibility": "fixed_time",
                "evidence_state": "verified",
            }
        ]
        return build_plan(
            trip_id=plan["trip_id"],
            generation=plan["generation"],
            state=state,
        )
    base = _single_day_plan(
        trip_id="phase63a-ready-lodging",
        city="Fixture",
        timezone_name="UTC",
    )
    state = copy.deepcopy(base["state"])
    location_id = "lodging-location-" + "c" * 64
    state["trip"]["lodgings"] = [
        {
            "lodging_id": "stay-a",
            "location_id": location_id,
            "check_in": "2026-07-29",
            "check_out": "2026-07-30",
            "kind": "hotel",
            "decision_state": "booked",
            "evidence_state": "unverified",
        }
    ]
    day = state["itinerary"]["days"][0]
    day.update(
        {
            "start_lodging_id": "stay-a",
            "end_lodging_id": "stay-a",
            "start_location_id": location_id,
            "end_location_id": location_id,
        }
    )
    day["places"][0]["location_id"] = location_id
    return build_plan(
        trip_id=base["trip_id"],
        generation=base["generation"],
        state=state,
    )


class Phase63ACanonicalLodgingReadinessTests(unittest.TestCase):
    def test_public_api_exports_exact_assessment_contract(self) -> None:
        plan = _lodging_plan(split=False)
        composed, assessment = _assessment(plan, _empty_snapshot())

        self.assertIs(
            CanonicalLodgingEvidenceAssessment,
            trip_planner.CanonicalLodgingEvidenceAssessment,
        )
        self.assertIs(
            assess_canonical_lodging_evidence,
            trip_planner.assess_canonical_lodging_evidence,
        )
        self.assertEqual(
            CANONICAL_LODGING_EVIDENCE_VERSION,
            trip_planner.CANONICAL_LODGING_EVIDENCE_VERSION,
        )
        self.assertIs(type(assessment), CanonicalLodgingEvidenceAssessment)
        self.assertEqual(composed.plan_revision, plan["revision"])
        for public_name in (
            "CANONICAL_LODGING_EVIDENCE_VERSION",
            "CanonicalLodgingEvidenceAssessment",
            "assess_canonical_lodging_evidence",
        ):
            self.assertIn(public_name, trip_planner.__all__)
        with self.assertRaises(TypeError):
            copy.copy(assessment)
        with self.assertRaises(TypeError):
            pickle.dumps(assessment)

    def test_empty_snapshot_keeps_single_and_split_lodging_unverified(
        self,
    ) -> None:
        for split, affected_count in ((False, 1), (True, 2)):
            with self.subTest(split=split):
                plan = _lodging_plan(split=split)
                snapshot = _empty_snapshot()
                composed, assessment = _assessment(plan, snapshot)
                readiness = assess_trip_readiness(
                    canonical_plan=plan,
                    composed=composed,
                    snapshot=snapshot,
                )

                self.assertFalse(assessment.ready)
                self.assertEqual((), assessment.used_observation_ids)
                self.assertIsNone(assessment.recheck_required_at)
                lodging = next(
                    item
                    for item in readiness.problems
                    if item.code == LODGING_PROBLEM
                )
                self.assertEqual(affected_count, lodging.affected_count)
                self.assertEqual(
                    assessment.assessment_id,
                    readiness.lodging_evidence_assessment_id,
                )

    def test_fresh_exact_identities_remove_only_lodging_problem(
        self,
    ) -> None:
        for split in (False, True):
            with self.subTest(split=split):
                plan = _lodging_plan(split=split)
                baseline = _readiness(plan, _empty_snapshot())
                observations = _identity_observations(plan)
                snapshot = _identity_snapshot(*observations)
                composed, assessment = _assessment(plan, snapshot)
                replay = assess_canonical_lodging_evidence(
                    canonical_plan=plan,
                    composed=composed,
                    snapshot=snapshot,
                )
                reverse_snapshot = _identity_snapshot(
                    *reversed(observations)
                )
                reverse_composed, reverse = _assessment(
                    plan,
                    reverse_snapshot,
                )
                readiness = assess_trip_readiness(
                    canonical_plan=plan,
                    composed=composed,
                    snapshot=snapshot,
                )

                self.assertTrue(assessment.ready)
                self.assertEqual(
                    tuple(
                        sorted(
                            item.observation_id for item in observations
                        )
                    ),
                    assessment.used_observation_ids,
                )
                self.assertEqual(
                    min(item.valid_until for item in observations),
                    assessment.recheck_required_at,
                )
                self.assertEqual(assessment, replay)
                self.assertEqual(
                    assessment.assessment_id,
                    replay.assessment_id,
                )
                self.assertEqual(composed, reverse_composed)
                self.assertEqual(assessment, reverse)
                self.assertEqual(
                    tuple(
                        item
                        for item in baseline.problems
                        if item.code != LODGING_PROBLEM
                    ),
                    readiness.problems,
                )
                self.assertNotIn(
                    LODGING_PROBLEM,
                    {item.code for item in readiness.problems},
                )
                self.assertEqual(
                    assessment.assessment_id,
                    readiness.lodging_evidence_assessment_id,
                )

    def test_missing_stale_and_custom_identity_policy_fail_closed(
        self,
    ) -> None:
        plan = _lodging_plan(split=False)
        location_id = _location_ids(plan)[0]
        policies = _identity_policies()
        stale_observation = _identity_observation(
            policies,
            location_id=location_id,
            place_id=f"{PRIVATE_PLACE_ID}-stale",
            retrieved_at=NOW - timedelta(days=2),
            valid_until=NOW - timedelta(days=1),
        )
        boundary_observation = _identity_observation(
            policies,
            location_id=location_id,
            place_id=f"{PRIVATE_PLACE_ID}-boundary",
            valid_until=NOW,
        )
        tampered_observation = _identity_observation(
            policies,
            location_id=location_id,
            place_id=f"{PRIVATE_PLACE_ID}-tampered",
        )
        tampered_snapshot = _identity_snapshot(tampered_observation)
        tampered_composed = compose_trip_state(plan, tampered_snapshot)
        object.__setattr__(
            tampered_observation.provenance,
            "response_id",
            "synthetic-unreviewed-response",
        )
        custom_snapshot, custom_observation = _custom_policy_snapshot(
            location_id
        )
        cases = (
            ("missing", _identity_snapshot()),
            ("stale", _identity_snapshot(stale_observation)),
            ("half-open-deadline", _identity_snapshot(boundary_observation)),
            ("custom-policy", custom_snapshot),
        )

        self.assertIs(
            EvidenceState.VERIFIED,
            custom_snapshot.resolve(custom_observation.key).evidence_state,
        )
        for label, snapshot in cases:
            with self.subTest(case=label):
                composed, assessment = _assessment(plan, snapshot)
                readiness = assess_trip_readiness(
                    canonical_plan=plan,
                    composed=composed,
                    snapshot=snapshot,
                )

                self.assertFalse(assessment.ready)
                self.assertIn(
                    LODGING_PROBLEM,
                    {item.code for item in readiness.problems},
                )
                self.assertEqual(
                    assessment.assessment_id,
                    readiness.lodging_evidence_assessment_id,
                )

        with self.assertRaises(ValueError):
            assess_canonical_lodging_evidence(
                canonical_plan=plan,
                composed=tampered_composed,
                snapshot=tampered_snapshot,
            )
        with self.assertRaises(ValueError):
            assess_trip_readiness(
                canonical_plan=plan,
                composed=tampered_composed,
                snapshot=tampered_snapshot,
            )

    def test_duplicate_canonical_location_uses_one_identity(self) -> None:
        plan = _duplicate_location_split_plan()
        self.assertEqual(2, len(plan["state"]["trip"]["lodgings"]))
        self.assertEqual(1, len(_location_ids(plan)))
        observation = _identity_observations(plan)[0]
        snapshot = _identity_snapshot(observation)
        _composed, assessment = _assessment(plan, snapshot)

        self.assertTrue(assessment.ready)
        self.assertEqual(
            (observation.observation_id,),
            assessment.used_observation_ids,
        )
        self.assertEqual(
            observation.valid_until,
            assessment.recheck_required_at,
        )

    def test_snapshot_drift_fails_closed(self) -> None:
        plan = _lodging_plan(split=False)
        location_id = _location_ids(plan)[0]
        first = _identity_observations(plan)[0]
        current = _identity_snapshot(
            _identity_observation(
                _identity_policies(),
                location_id=location_id,
                place_id=f"{PRIVATE_PLACE_ID}-current",
            )
        )
        stale_composed = compose_trip_state(plan, _identity_snapshot(first))
        with self.assertRaises(ValueError):
            assess_canonical_lodging_evidence(
                canonical_plan=plan,
                composed=stale_composed,
                snapshot=current,
            )

    def test_forged_oversized_validity_and_stale_aggregates_fail_closed(
        self,
    ) -> None:
        plan = _fully_ready_lodging_plan()
        original = _identity_observations(plan)[0]
        snapshot = _identity_snapshot(original)
        composed = compose_trip_state(plan, snapshot)
        forged = FactObservation(
            key=original.key,
            value=original.value,
            provenance=original.provenance,
            retrieved_at=original.retrieved_at,
            valid_until=original.retrieved_at + timedelta(days=500),
            purge_at=original.purge_at,
            confidence=original.confidence,
        )
        policy = snapshot.policies.policy(
            forged.provenance.retention_policy_id
        )
        object.__setattr__(snapshot, "observations", (forged,))

        self.assertGreater(
            (forged.valid_until - forged.retrieved_at).total_seconds(),
            policy.max_validity_seconds,
        )
        self.assertNotEqual(original.observation_id, forged.observation_id)
        with self.assertRaises(ValueError):
            assess_canonical_lodging_evidence(
                canonical_plan=plan,
                composed=composed,
                snapshot=snapshot,
            )
        with self.assertRaises(ValueError):
            assess_trip_readiness(
                canonical_plan=plan,
                composed=composed,
                snapshot=snapshot,
            )
        with self.assertRaises(FactContractError) as endpoint_error:
            extract_fresh_google_place_endpoint(
                snapshot,
                _location_ids(plan)[0],
            )
        self.assertEqual("CACHE_CORRUPTED", endpoint_error.exception.code)

    def test_fresh_at_monkeypatch_cannot_bypass_half_open_deadline(
        self,
    ) -> None:
        plan = _lodging_plan(split=False)
        observation = _identity_observation(
            _identity_policies(),
            location_id=_location_ids(plan)[0],
            place_id=f"{PRIVATE_PLACE_ID}-fresh-at-hostile",
            valid_until=NOW,
        )
        snapshot = _identity_snapshot(observation)
        composed = compose_trip_state(plan, snapshot)

        with patch.object(
            FactObservation,
            "fresh_at",
            return_value=True,
        ) as hostile_fresh_at:
            assessment = assess_canonical_lodging_evidence(
                canonical_plan=plan,
                composed=composed,
                snapshot=snapshot,
            )
            readiness = assess_trip_readiness(
                canonical_plan=plan,
                composed=composed,
                snapshot=snapshot,
            )

        self.assertEqual(0, hostile_fresh_at.call_count)
        self.assertFalse(assessment.ready)
        self.assertIn(
            LODGING_PROBLEM,
            {item.code for item in readiness.problems},
        )

    def test_hostile_mapping_is_detached_once_before_assessment(self) -> None:
        plan = _fully_ready_lodging_plan()
        snapshot = _identity_snapshot(*_identity_observations(plan))
        composed = compose_trip_state(plan, snapshot)
        expected_assessment = assess_canonical_lodging_evidence(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
        )
        expected_readiness = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
        )
        assessment_source = _OneShotPlanMapping(plan)
        readiness_source = _OneShotPlanMapping(plan)

        assessment = assess_canonical_lodging_evidence(
            canonical_plan=assessment_source,
            composed=composed,
            snapshot=snapshot,
        )
        readiness = assess_trip_readiness(
            canonical_plan=readiness_source,
            composed=composed,
            snapshot=snapshot,
        )

        self.assertEqual(expected_assessment, assessment)
        self.assertEqual(expected_readiness, readiness)
        self.assertEqual(1, assessment_source.items_calls)
        self.assertEqual(0, assessment_source.unexpected_reads)
        self.assertEqual(1, readiness_source.items_calls)
        self.assertEqual(0, readiness_source.unexpected_reads)

    def test_evil_string_key_fails_exact_tree_preflight_before_lookup(
        self,
    ) -> None:
        plan = _fully_ready_lodging_plan()
        snapshot = _identity_snapshot(*_identity_observations(plan))
        composed = compose_trip_state(plan, snapshot)
        baseline = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
        )
        hostile_calls: list[str] = []

        self.assertEqual("travel_ready", baseline.status.value)
        for assessor_name in ("lodging_evidence", "trip_readiness"):
            with self.subTest(assessor=assessor_name):
                hostile_plan = copy.deepcopy(plan)
                lodging = hostile_plan["state"]["trip"]["lodgings"][0]
                location_id = lodging.pop("location_id")
                lodging[_EvilKey("location_id", hostile_calls)] = location_id

                with self.assertRaisesRegex(
                    TypeError,
                    "canonical_plan keys must be exact strings",
                ):
                    if assessor_name == "lodging_evidence":
                        assess_canonical_lodging_evidence(
                            canonical_plan=hostile_plan,
                            composed=composed,
                            snapshot=snapshot,
                        )
                    else:
                        assess_trip_readiness(
                            canonical_plan=hostile_plan,
                            composed=composed,
                            snapshot=snapshot,
                        )

        self.assertEqual([], hostile_calls)

    def test_safe_output_never_contains_private_identity_material(
        self,
    ) -> None:
        plan = _lodging_plan(split=False)
        observation = _identity_observations(plan)[0]
        _composed, assessment = _assessment(
            plan,
            _identity_snapshot(observation),
        )
        readiness = _readiness(plan, _identity_snapshot(observation))
        safe = assessment.to_safe_dict()
        encoded = json.dumps(
            {
                "assessment": safe,
                "assessment_repr": repr(assessment),
                "readiness": readiness.to_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        self.assertEqual(
            CANONICAL_LODGING_EVIDENCE_VERSION,
            safe["contract_version"],
        )
        self.assertTrue(safe["ready"])
        self.assertEqual(1, safe["used_evidence_count"])
        self.assertNotIn("used_observation_ids", safe)
        for forbidden in (
            _location_ids(plan)[0],
            observation.value.payload["provider_place_id"],
            observation.observation_id,
        ):
            self.assertNotIn(forbidden, encoded)

    def test_snapshot_resolve_monkeypatch_is_not_dynamically_dispatched(
        self,
    ) -> None:
        plan = _lodging_plan(split=False)
        observation = _identity_observations(plan)[0]
        snapshot = _identity_snapshot(observation)
        composed = compose_trip_state(plan, snapshot)
        calls: list[FactKey] = []

        def hostile_resolve(_snapshot, key):
            calls.append(key)
            raise AssertionError("hostile dynamic resolve dispatch")

        with patch.object(EvidenceSnapshot, "resolve", hostile_resolve):
            assessment = assess_canonical_lodging_evidence(
                canonical_plan=plan,
                composed=composed,
                snapshot=snapshot,
            )
            readiness = assess_trip_readiness(
                canonical_plan=plan,
                composed=composed,
                snapshot=snapshot,
            )

        self.assertEqual([], calls)
        self.assertTrue(assessment.ready)
        self.assertNotIn(
            LODGING_PROBLEM,
            {item.code for item in readiness.problems},
        )

    def test_identity_batch_has_no_unchecked_endpoint_core(self) -> None:
        self.assertFalse(
            hasattr(
                places_identity_module,
                "_extract_fresh_google_place_endpoint_from_validated_snapshot",
            )
        )
        plan = _lodging_plan(split=True)
        observations = _identity_observations(plan)
        binding, outcomes = (
            places_identity_module._extract_fresh_google_place_endpoint_batch(
                _identity_snapshot(observations[0]),
                (
                    _location_ids(plan)[1],
                    _location_ids(plan)[0],
                    _location_ids(plan)[0],
                ),
            )
        )

        self.assertEqual(7, len(binding))
        self.assertEqual(
            tuple(sorted(set(_location_ids(plan)))),
            tuple(item[0] for item in outcomes),
        )
        self.assertEqual(
            {"PENDING_REVIEW", "VERIFIED"},
            {item[1] for item in outcomes},
        )
        with self.assertRaises(FactContractError) as too_many:
            places_identity_module._extract_fresh_google_place_endpoint_batch(
                _identity_snapshot(observations[0]),
                (_location_ids(plan)[0],) * 4097,
            )
        self.assertEqual("INVALID_PROVIDER_REQUEST", too_many.exception.code)
        with self.assertRaises(FactContractError) as subclassed:
            places_identity_module._extract_fresh_google_place_endpoint_batch(
                _identity_snapshot(observations[0]),
                (_EvilText(_location_ids(plan)[0]),),
            )
        self.assertEqual(
            "INVALID_PROVIDER_REQUEST",
            subclassed.exception.code,
        )

    def test_enum_value_properties_are_not_dynamically_dispatched(self) -> None:
        plan = _fully_ready_lodging_plan()
        observation = _identity_observations(plan)[0]
        snapshot = _identity_snapshot(observation)
        calls: list[str] = []

        def hostile_kind_value(_value):
            calls.append("kind")
            raise AssertionError("dynamic FactKind.value dispatch")

        def hostile_persistence_value(_value):
            calls.append("persistence")
            raise AssertionError("dynamic EvidencePersistence.value dispatch")

        with patch.object(
            FactKind,
            "value",
            property(hostile_kind_value),
            create=True,
        ):
            with patch.object(
                EvidencePersistence,
                "value",
                property(hostile_persistence_value),
                create=True,
            ):
                _binding, outcomes = (
                    places_identity_module._extract_fresh_google_place_endpoint_batch(
                        snapshot,
                        (_location_ids(plan)[0],),
                    )
                )

        endpoint = extract_fresh_google_place_endpoint(
            snapshot,
            _location_ids(plan)[0],
        )

        self.assertEqual([], calls)
        self.assertEqual("VERIFIED", outcomes[0][1])
        self.assertEqual(
            observation.value.payload["provider_place_id"],
            endpoint.provider_place_id,
        )

    def test_facts_normalizers_are_not_dynamically_dispatched(self) -> None:
        plan = _fully_ready_lodging_plan()
        observation = _identity_observations(plan)[0]
        snapshot = _identity_snapshot(observation)
        calls: list[str] = []

        def hostile(*_args, **_kwargs):
            calls.append("facts-normalizer")
            raise AssertionError("dynamic facts normalizer dispatch")

        def hostile_re_sub(*_args, **_kwargs):
            calls.append("integrity-re-sub")
            raise AssertionError("dynamic regex dispatch")

        normalizers = (
            "_normalize_payload",
            "_normalize_place_identity",
            "_normalize_place_profile",
            "_normalize_opening_hours",
            "_normalize_route",
            "_normalized_public_uri",
        )
        patches = [
            patch.object(facts_module, name, hostile)
            for name in normalizers
        ]
        entered = []
        try:
            for patcher in patches:
                entered.append(patcher)
                patcher.start()
            with patch.object(
                places_identity_module.validate_evidence_snapshot_integrity.__globals__[
                    "re"
                ],
                "sub",
                hostile_re_sub,
            ):
                _binding, outcomes = (
                    places_identity_module._extract_fresh_google_place_endpoint_batch(
                        snapshot,
                        (_location_ids(plan)[0],),
                    )
                )
        finally:
            for patcher in reversed(entered):
                patcher.stop()

        self.assertEqual([], calls)
        self.assertEqual("VERIFIED", outcomes[0][1])
        endpoint = extract_fresh_google_place_endpoint(
            snapshot,
            _location_ids(plan)[0],
        )
        self.assertEqual(
            observation.value.payload["provider_place_id"],
            endpoint.provider_place_id,
        )

    def test_resealed_secret_identity_cannot_use_patched_facts_schema(
        self,
    ) -> None:
        plan = _fully_ready_lodging_plan()
        observation = _identity_observations(plan)[0]
        snapshot = _identity_snapshot(observation)
        hostile_place_id = "api_key=synthetic-secret-sentinel"
        hostile_payload = {"provider_place_id": hostile_place_id}
        hostile_json = facts_module._canonical_json(hostile_payload)
        hostile_value_digest = facts_module._digest(
            {
                "kind": "place_identity",
                "schema_version": observation.value.schema_version,
                "payload": hostile_payload,
            },
            prefix="fact-value",
        )
        object.__setattr__(observation.value, "canonical_json", hostile_json)
        object.__setattr__(
            observation.value,
            "value_digest",
            hostile_value_digest,
        )
        object.__setattr__(
            observation.provenance,
            "provider_record_id",
            hostile_place_id,
        )
        hostile_observation_id = facts_module._digest(
            {
                "contract_version": observation.contract_version,
                "key": observation.key.to_dict(),
                "value": {
                    "kind": "place_identity",
                    "schema_version": observation.value.schema_version,
                    "payload": hostile_payload,
                    "value_digest": hostile_value_digest,
                },
                "provenance": observation.provenance._identity_payload(),
                "retrieved_at": observation.retrieved_at.isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "valid_until": observation.valid_until.isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "purge_at": None,
                "confidence": observation.confidence,
            },
            prefix="fact-observation",
        )
        object.__setattr__(
            observation,
            "observation_id",
            hostile_observation_id,
        )
        hostile_evidence_revision = facts_module._digest(
            {"observation_ids": [hostile_observation_id]},
            prefix="active-evidence",
        )
        object.__setattr__(
            snapshot,
            "evidence_revision",
            hostile_evidence_revision,
        )
        snapshot_payload = {
            "contract_version": snapshot.contract_version,
            "policy_registry_revision": snapshot.policies.revision,
            "store_revision": snapshot.store_revision,
            "evidence_revision": hostile_evidence_revision,
            "evaluation_at": snapshot.evaluation_at.isoformat().replace(
                "+00:00",
                "Z",
            ),
            "purge_checked_at": snapshot.purge_checked_at.isoformat().replace(
                "+00:00",
                "Z",
            ),
        }
        if snapshot.outcome_revision is not None:
            snapshot_payload["outcome_revision"] = snapshot.outcome_revision
        object.__setattr__(
            snapshot,
            "snapshot_id",
            facts_module._digest(
                snapshot_payload,
                prefix="evidence-snapshot",
            ),
        )
        calls: list[str] = []

        def permissive(payload):
            calls.append("place-identity")
            return payload

        with patch.object(
            facts_module,
            "_normalize_place_identity",
            permissive,
        ):
            with self.assertRaises(FactContractError) as caught:
                extract_fresh_google_place_endpoint(
                    snapshot,
                    _location_ids(plan)[0],
                )

        self.assertEqual([], calls)
        self.assertEqual("CACHE_CORRUPTED", caught.exception.code)

    def test_integrity_projection_accepts_all_four_factory_fact_kinds(
        self,
    ) -> None:
        identity_key = place_identity_key()
        identity = google_identity_observation(
            ALL_FACT_POLICIES,
            google_identity_request(ALL_FACT_POLICIES, identity_key),
            identity_key,
        )
        profile_key = place_profile_key()
        profile = FactObservation(
            key=profile_key,
            value=FactValue.from_payload(
                FactKind.PLACE_PROFILE,
                {
                    "provider_place_id": "place-123",
                    "latitude": 35.0,
                    "longitude": 139.0,
                    "display_name": "Synthetic place",
                    "timezone": "Asia/Tokyo",
                    "business_status": "operational",
                },
            ),
            provenance=fact_provenance(
                profile_key,
                provider="google-places",
            ),
            retrieved_at=NOW,
            valid_until=NOW + timedelta(hours=2),
            purge_at=NOW + timedelta(hours=12),
            confidence=1.0,
        )
        hours_key = opening_key(basis="regular_typical")
        hours = FactObservation(
            key=hours_key,
            value=opening_value(basis="regular_typical"),
            provenance=fact_provenance(
                hours_key,
                provider="google-places",
            ),
            retrieved_at=NOW,
            valid_until=NOW + timedelta(hours=2),
            purge_at=NOW + timedelta(hours=12),
            confidence=1.0,
        )
        route = route_observation()
        ledger = EvidenceLedger(
            ALL_FACT_POLICIES,
            (identity, profile, hours, route),
            generation=4,
            _token=facts_module._LEDGER_TOKEN,
        )
        snapshot = EvidenceSnapshot.from_ledger(
            ledger,
            evaluation_at=NOW,
            purge_now=NOW,
        )

        projection = validate_evidence_snapshot_integrity(snapshot)

        self.assertEqual(4, len(projection[9]))
        self.assertEqual(
            {
                "place_identity",
                "place_profile",
                "place_opening_hours",
                "route_estimate",
            },
            {item[2][0] for item in projection[9]},
        )

    def test_uri_regex_and_extreme_hours_fail_with_fixed_error_shape(
        self,
    ) -> None:
        route = route_observation()
        object.__setattr__(
            route.provenance,
            "source_uri",
            "https://example.test/path?api%5Fkey=synthetic",
        )
        _reseal_observation(route)
        route_snapshot = _reseal_snapshot(
            ALL_FACT_POLICIES,
            (route,),
            evaluation_at=NOW,
            purge_checked_at=NOW,
        )
        regex_calls: list[str] = []

        def permissive_regex(*_args, **_kwargs):
            regex_calls.append("sub")
            return "ordinary"

        integrity_globals = validate_evidence_snapshot_integrity.__globals__
        with patch.object(integrity_globals["re"], "sub", permissive_regex):
            with self.assertRaisesRegex(
                ValueError,
                "snapshot integrity verification failed",
            ):
                validate_evidence_snapshot_integrity(route_snapshot)
        self.assertEqual([], regex_calls)

        hours_key = opening_key(basis="regular_typical")
        hours = FactObservation(
            key=hours_key,
            value=opening_value(basis="regular_typical"),
            provenance=fact_provenance(
                hours_key,
                provider="google-places",
            ),
            retrieved_at=NOW,
            valid_until=NOW + timedelta(hours=2),
            purge_at=NOW + timedelta(hours=12),
            confidence=1.0,
        )
        extreme_payload = {
            "provider_place_id": "place-123",
            "timezone": "America/New_York",
            "basis": "regular_typical",
            "coverage_start": "0001-01-01",
            "coverage_end": "0001-01-01",
            "intervals": [
                {
                    "start_at": "0001-01-01T00:00:00+14:00",
                    "end_at": "0001-01-01T01:00:00+14:00",
                }
            ],
            "closed_dates": [],
        }
        extreme_json = facts_module._canonical_json(extreme_payload)
        object.__setattr__(hours.value, "canonical_json", extreme_json)
        object.__setattr__(
            hours.value,
            "value_digest",
            facts_module._digest(
                {
                    "kind": "place_opening_hours",
                    "schema_version": hours.value.schema_version,
                    "payload": extreme_payload,
                },
                prefix="fact-value",
            ),
        )
        _reseal_observation(hours)
        hours_snapshot = _reseal_snapshot(
            ALL_FACT_POLICIES,
            (hours,),
            evaluation_at=NOW,
            purge_checked_at=NOW,
        )
        with self.assertRaisesRegex(
            ValueError,
            "snapshot integrity verification failed",
        ):
            validate_evidence_snapshot_integrity(hours_snapshot)

    def test_constructor_and_slot_monkeypatches_cannot_bypass_projection(
        self,
    ) -> None:
        plan = _fully_ready_lodging_plan()
        original = _identity_observations(plan)[0]
        snapshot = _identity_snapshot(original)
        location_id = _location_ids(plan)[0]
        slot_calls: list[str] = []

        def hostile_value_slot(_observation):
            slot_calls.append("value")
            raise AssertionError("dynamic FactObservation.value dispatch")

        with patch.object(
            FactObservation,
            "value",
            property(hostile_value_slot),
        ):
            endpoint = extract_fresh_google_place_endpoint(
                snapshot,
                location_id,
            )

        self.assertEqual([], slot_calls)
        self.assertEqual(
            original.value.payload["provider_place_id"],
            endpoint.provider_place_id,
        )

        replacement = _identity_observation(
            _identity_policies(),
            location_id=location_id,
            place_id=f"{PRIVATE_PLACE_ID}-replacement",
        )
        object.__setattr__(original, "value", replacement.value)
        with patch.object(FactObservation, "__post_init__", return_value=None):
            with self.assertRaises(FactContractError) as caught:
                extract_fresh_google_place_endpoint(snapshot, location_id)
        self.assertEqual("CACHE_CORRUPTED", caught.exception.code)

    def test_retention_callback_cannot_substitute_lodging_identity(self) -> None:
        plan = _fully_ready_lodging_plan()
        original = _identity_observations(plan)[0]
        snapshot = _identity_snapshot(original)
        location_id = _location_ids(plan)[0]
        replacement = _identity_observation(
            _identity_policies(),
            location_id=location_id,
            place_id=f"{PRIVATE_PLACE_ID}-substitute",
        )
        calls: list[str] = []

        def hostile_retained(observation, _checked_at):
            calls.append("retained_at")
            object.__setattr__(observation, "value", replacement.value)
            object.__setattr__(
                observation,
                "provenance",
                replacement.provenance,
            )
            object.__setattr__(
                observation,
                "observation_id",
                replacement.observation_id,
            )
            return True

        with patch.object(
            FactObservation,
            "retained_at",
            hostile_retained,
        ):
            endpoint = extract_fresh_google_place_endpoint(
                snapshot,
                location_id,
            )
            readiness = _readiness(plan, snapshot)
            review = prepare_private_delivery_review(
                encode_plan(plan),
                snapshot,
                profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
                source_path=SOURCE_PATH,
                target_path=TARGET_PATH,
                clock=lambda: NOW,
            )

        self.assertEqual([], calls)
        self.assertEqual(
            original.value.payload["provider_place_id"],
            endpoint.provider_place_id,
        )
        self.assertEqual("travel_ready", readiness.status.value)
        self.assertFalse(review.to_safe_dict()["writes_performed"])
        self.assertNotEqual(
            replacement.value.payload["provider_place_id"],
            endpoint.provider_place_id,
        )

    def test_all_other_gates_plus_identity_can_reach_travel_ready(self) -> None:
        for split, expected_stays in ((False, 1), (True, 2)):
            with self.subTest(split=split):
                plan = _fully_ready_lodging_plan(split=split)
                before = copy.deepcopy(plan)
                observations = _identity_observations(plan)
                snapshot = _identity_snapshot(*observations)
                readiness = _readiness(plan, snapshot)
                review = prepare_private_delivery_review(
                    encode_plan(plan),
                    snapshot,
                    profile=(
                        PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE
                    ),
                    source_path=SOURCE_PATH,
                    target_path=TARGET_PATH,
                    clock=lambda: NOW,
                )

                self.assertEqual("travel_ready", readiness.status.value)
                self.assertEqual("none", readiness.next_action.value)
                self.assertEqual((), readiness.problems)
                self.assertEqual(expected_stays, readiness.used_evidence_count)
                self.assertEqual(
                    min(item.valid_until for item in observations),
                    readiness.recheck_required_at,
                )
                self.assertEqual(
                    ("index.html", "calendar.ics", "manifest.json"),
                    tuple(item.filename for item in review.artifacts),
                )
                self.assertFalse(review.to_safe_dict()["writes_performed"])
                self.assertEqual(before, plan)
                self.assertEqual(
                    {"unverified"},
                    {
                        item["evidence_state"]
                        for item in plan["state"]["trip"]["lodgings"]
                    },
                )

    def test_ready_delivery_binds_v2_readiness_and_lodging_deadline(self) -> None:
        plan = _fully_ready_lodging_plan()
        observation = _identity_observations(
            plan,
            first_valid_for=timedelta(minutes=20),
        )[0]
        snapshot = _identity_snapshot(observation)
        review = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=lambda: NOW,
        )
        assert review.readiness is not None
        manifest = json.loads(
            next(
                item.payload
                for item in review.artifacts
                if item.filename == "manifest.json"
            )
        )

        self.assertEqual(observation.valid_until, review.expires_at)
        self.assertEqual(
            review.readiness.lodging_evidence_assessment_id,
            _assessment(plan, snapshot)[1].assessment_id,
        )
        self.assertEqual(
            "trip-readiness/v2",
            manifest["readiness"]["readiness_contract_version"],
        )
        self.assertEqual(
            review.readiness.readiness_id,
            manifest["readiness"]["readiness_id"],
        )
        self.assertEqual(
            PRIVATE_DELIVERY_MANIFEST_VERSION,
            manifest["contract_version"],
        )
        self.assertNotIn(
            "lodging_evidence_assessment_id",
            manifest["readiness"],
        )

    def test_readiness_assessment_tamper_invalidates_delivery_review(self) -> None:
        plan = _fully_ready_lodging_plan()
        snapshot = _identity_snapshot(*_identity_observations(plan))
        review = prepare_private_delivery_review(
            encode_plan(plan),
            snapshot,
            profile=PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE,
            source_path=SOURCE_PATH,
            target_path=TARGET_PATH,
            clock=lambda: NOW,
        )
        assert review.readiness is not None
        object.__setattr__(
            review.readiness,
            "lodging_evidence_assessment_id",
            "f" * 64,
        )

        with self.assertRaises(PrivateDeliveryError) as caught:
            review.to_safe_dict()
        self.assertEqual(
            "PRIVATE_DELIVERY_REVIEW_TAMPERED",
            caught.exception.code,
        )


if __name__ == "__main__":
    unittest.main()
