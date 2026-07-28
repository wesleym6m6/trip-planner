"""Offline contract tests for the Phase 4.2 Google Places identity boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trip_planner.evidence_store import EvidenceStore
from trip_planner.facts import (
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
    AuthorizedProviderResult,
    EvidenceLedger,
    EvidenceSnapshot,
    FactContractError,
    FactKind,
    FactObservation,
    FactValue,
    ProviderProvenance,
    ProviderRequest,
    ProviderProblemCode,
    ProviderResult,
    ProviderResultStatus,
    authorize_provider_result,
    google_maps_policy_registry,
    merge_provider_result,
)
from trip_planner.places_identity import (
    GOOGLE_PLACE_IDENTITY_FIELD_MASK,
    GOOGLE_PLACE_ID_REFRESH_FIELD_MASK,
    PlaceCandidateRejection,
    PlaceEndpointIdentity,
    PlaceIdentityCandidateAssessment,
    PlaceIdentityIntent,
    PlaceIdentityRequest,
    PlaceIdentityReview,
    PlaceIdentityReviewAuthority,
    PlaceIdentityReviewGrant,
    PlaceIdentityReviewStatus,
    build_google_place_identity_refresh_request,
    build_google_place_identity_request,
    evaluate_google_place_identity_candidates,
    extract_fresh_google_place_endpoint,
    finalize_google_place_identity_refresh,
    finalize_google_place_identity_review,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
LOCATION_ID = "location-busan-museum"
PLACE_ID = "ChIJ-busan-museum"
OTHER_PLACE_ID = "ChIJ-busan-museum-annex"
SENTINEL_NAME = "Restricted Sentinel Museum"
SENTINEL_ADDRESS = "987 Restricted Sentinel Road"
SENTINEL_QUERY = "RESTRICTED_QUERY_SENTINEL"

EXPECTED_SEARCH_FIELD_MASK = (
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.primaryType",
    "places.types",
    "places.addressComponents",
)


def intent(**overrides: object) -> PlaceIdentityIntent:
    values: dict[str, object] = {
        "location_id": LOCATION_ID,
        "text_query": f"Busan Museum Busan KR {SENTINEL_QUERY}",
        "expected_name": "Busan Museum",
        "region_code": "KR",
        "language_code": "en",
        "expected_locality": "Busan",
        "expected_primary_types": ("museum",),
        "latitude": 35.1379,
        "longitude": 129.0915,
        "radius_m": 25_000,
    }
    values.update(overrides)
    return PlaceIdentityIntent(**values)


def address_component(
    *,
    long_text: str,
    short_text: str,
    component_type: str,
) -> dict[str, object]:
    return {
        "longText": long_text,
        "shortText": short_text,
        "types": [component_type, "political"],
        "languageCode": "en",
    }


def candidate(
    *,
    place_id: str = PLACE_ID,
    name: str = "Busan Museum",
    country_code: str = "KR",
    country_name: str = "South Korea",
    locality: str = "Busan",
    primary_type: str = "museum",
    types: list[str] | None = None,
    latitude: float = 35.1379,
    longitude: float = 129.0915,
    formatted_address: str = "63 UN pyeonghwa-ro, Busan, South Korea",
) -> dict[str, object]:
    normalized_types = (
        [primary_type, "tourist_attraction"]
        if types is None
        else list(types)
    )
    return {
        "id": place_id,
        "displayName": {"text": name, "languageCode": "en"},
        "formattedAddress": formatted_address,
        "location": {
            "latitude": latitude,
            "longitude": longitude,
        },
        "primaryType": primary_type,
        "types": normalized_types,
        "addressComponents": [
            address_component(
                long_text=locality,
                short_text=locality,
                component_type="locality",
            ),
            address_component(
                long_text=country_name,
                short_text=country_code,
                component_type="country",
            ),
        ],
    }


def response(*candidates: dict[str, object]) -> dict[str, object]:
    return {"places": list(candidates)}


def raw_result(authorized: AuthorizedProviderResult) -> ProviderResult:
    if type(authorized) is not AuthorizedProviderResult:
        raise AssertionError("identity finalizer must return authorized evidence")
    return authorized.result


def observation_payload(
    authorized: AuthorizedProviderResult,
) -> dict[str, object]:
    return raw_result(authorized).observations[0].value.payload


def unreviewed_identity_result(
    request: ProviderRequest,
    place_id: str,
    *,
    completed_at: datetime,
) -> ProviderResult:
    value = FactValue.from_payload(
        FactKind.PLACE_IDENTITY,
        {"provider_place_id": place_id},
    )
    observation = FactObservation(
        key=request.fact_keys[0],
        value=value,
        provenance=ProviderProvenance(
            provider_id=request.provider_id,
            adapter_id=request.adapter_id,
            adapter_version=request.adapter_version,
            request_fingerprint=request.request_fingerprint,
            retention_policy_id=request.policy_id,
            provider_record_id=place_id,
            response_id=None,
            source_uri=None,
            attributions=(("Google Maps", None),),
        ),
        retrieved_at=completed_at,
        valid_until=completed_at + timedelta(days=365),
        purge_at=None,
        confidence=1.0,
    )
    return ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=ProviderResultStatus.SUCCESS,
        observations=(observation,),
        problems=(),
        attempts_used=1,
        completed_at=completed_at,
    )


class PlacesIdentityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policies = google_maps_policy_registry(
            GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
        )
        self.empty_snapshot = EvidenceSnapshot.from_ledger(
            EvidenceLedger(self.policies),
            evaluation_at=NOW,
            purge_now=NOW,
        )

    def assert_contract_error(self, code: str, function) -> FactContractError:
        with self.assertRaises(FactContractError) as caught:
            function()
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def build_request(self, **intent_overrides: object):
        snapshot = intent_overrides.pop(
            "snapshot",
            self.empty_snapshot,
        )
        if type(snapshot) is not EvidenceSnapshot:
            raise AssertionError("test snapshot must be exact")
        return build_google_place_identity_request(
            intent(**intent_overrides),
            snapshot,
        )

    def evaluate(self, raw_response: dict[str, object], **intent_overrides):
        return evaluate_google_place_identity_candidates(
            self.build_request(**intent_overrides),
            raw_response,
            completed_at=NOW,
        )

    def finalize_review(
        self,
        review,
        grant=None,
        *,
        current_snapshot: EvidenceSnapshot | None = None,
        promotion_at: datetime = NOW + timedelta(minutes=1),
    ):
        authority = PlaceIdentityReviewAuthority(
            reviewer_id="trusted-test-host",
            clock=lambda: promotion_at,
        )
        return finalize_google_place_identity_review(
            review,
            (
                review.request.snapshot
                if current_snapshot is None
                else current_snapshot
            ),
            authority,
            grant,
        )

    def issue_grant(
        self,
        review,
        candidate_id: str,
        *,
        approved_at: datetime = NOW + timedelta(minutes=1),
    ) -> PlaceIdentityReviewGrant:
        authority = PlaceIdentityReviewAuthority(
            reviewer_id="human-reviewer",
            clock=lambda: approved_at,
        )
        return authority.issue_grant(review, candidate_id)

    def test_request_uses_exact_minimal_field_mask_and_binds_match_scope(
        self,
    ) -> None:
        request = self.build_request()

        self.assertEqual(
            EXPECTED_SEARCH_FIELD_MASK,
            GOOGLE_PLACE_IDENTITY_FIELD_MASK,
        )
        self.assertEqual("id", GOOGLE_PLACE_ID_REFRESH_FIELD_MASK)
        self.assertEqual(
            EXPECTED_SEARCH_FIELD_MASK,
            request.field_mask,
        )
        scope = dict(request.provider_request.query_scope)
        self.assertEqual(
            ",".join(EXPECTED_SEARCH_FIELD_MASK),
            scope["field_mask"],
        )
        self.assertEqual("Busan Museum", scope["expected_name"])
        self.assertEqual("Busan", scope["expected_locality"])
        self.assertEqual("museum", scope["expected_primary_types"])
        self.assertEqual(35.1379, scope["latitude"])
        self.assertEqual(129.0915, scope["longitude"])
        self.assertEqual(25_000.0, scope["radius_m"])
        self.assertEqual(5, scope["page_size"])

        baseline = request.provider_request.request_fingerprint
        variants = (
            {"expected_name": "Busan Modern History Museum"},
            {"expected_primary_types": ("art_gallery",)},
            {"expected_locality": "Seoul"},
            {"radius_m": 24_999},
            {"latitude": 35.2},
            {"text_query": "Busan Museum alternate query"},
        )
        for overrides in variants:
            with self.subTest(overrides=overrides):
                changed = self.build_request(**overrides)
                self.assertNotEqual(
                    baseline,
                    changed.provider_request.request_fingerprint,
                )

        safe = json.dumps(
            {
                "repr": repr(request),
                "binding": request.to_binding_dict(),
            },
            ensure_ascii=False,
        )
        self.assertNotIn(SENTINEL_QUERY, safe)
        self.assertNotIn("Busan Museum Busan KR", safe)
        self.assertNotIn("35.1379", safe)

    def test_generic_authorization_rejects_unreviewed_identity_seed(
        self,
    ) -> None:
        request = self.build_request().provider_request
        unreviewed = unreviewed_identity_result(
            request,
            "ChIJ-never-reviewed",
            completed_at=NOW,
        )

        self.assert_contract_error(
            "PENDING_REVIEW",
            lambda: authorize_provider_result(
                request,
                unreviewed,
                self.policies,
            ),
        )

    def test_review_graph_cannot_be_minted_outside_the_evaluator(
        self,
    ) -> None:
        reviewed = self.evaluate(response(candidate()))
        assessment = reviewed.assessments[0]

        self.assert_contract_error(
            "UNTRUSTED_PROVENANCE",
            lambda: PlaceIdentityCandidateAssessment(
                candidate=assessment.candidate,
                exact_name_match=True,
                rejection_codes=(),
                distance_m=assessment.distance_m,
            ),
        )
        self.assert_contract_error(
            "UNTRUSTED_PROVENANCE",
            lambda: PlaceIdentityReview(
                request=reviewed.request,
                assessments=reviewed.assessments,
                status=reviewed.status,
                recommended_candidate_id=(
                    reviewed.recommended_candidate_id
                ),
                completed_at=reviewed.completed_at,
                expires_at=reviewed.expires_at,
                attempts_used=reviewed.attempts_used,
                results_truncated=reviewed.results_truncated,
            ),
        )

    def test_unique_exact_match_is_ready_even_when_not_first(self) -> None:
        wrong_first = candidate(
            place_id="ChIJ-wrong-country",
            country_code="JP",
            country_name="Japan",
            locality="Fukuoka",
            latitude=33.5902,
            longitude=130.4017,
        )
        correct_second = candidate()

        review = self.evaluate(response(wrong_first, correct_second))

        self.assertIs(PlaceIdentityReviewStatus.READY, review.status)
        recommended = next(
            item
            for item in review.assessments
            if item.candidate.candidate_id
            == review.recommended_candidate_id
        )
        self.assertEqual(
            PLACE_ID,
            recommended.candidate.provider_place_id,
        )
        self.assertTrue(recommended.eligible)
        result = self.finalize_review(review)
        self.assertEqual({"provider_place_id": PLACE_ID}, observation_payload(result))

    def test_ambiguous_candidates_require_an_exact_bound_grant(self) -> None:
        first = candidate(
            place_id=PLACE_ID,
            name="Busan Museum Main Hall",
        )
        second = candidate(
            place_id=OTHER_PLACE_ID,
            name="Busan Museum Annex",
            latitude=35.14,
            longitude=129.09,
        )
        review = self.evaluate(response(first, second))

        self.assertIs(
            PlaceIdentityReviewStatus.REVIEW_REQUIRED,
            review.status,
        )
        self.assertIsNone(review.recommended_candidate_id)
        self.assertEqual(
            [{"label": "Google Maps", "uri": None}],
            review.to_review_payload()["attributions"],
        )
        self.assertEqual(2, len(review.eligible_assessments))
        self.assert_contract_error(
            "PENDING_REVIEW",
            lambda: self.finalize_review(review),
        )

        selected = next(
            item
            for item in review.eligible_assessments
            if item.candidate.provider_place_id == OTHER_PLACE_ID
        )
        grant = self.issue_grant(
            review,
            selected.candidate.candidate_id,
        )
        result = self.finalize_review(review, grant)
        self.assertEqual(
            {"provider_place_id": OTHER_PLACE_ID},
            observation_payload(result),
        )

        self.assert_contract_error(
            "PENDING_REVIEW",
            lambda: PlaceIdentityReviewGrant(
                review_id=review.review_id,
                candidate_set_digest=review.candidate_set_digest,
                candidate_id=selected.candidate.candidate_id,
                reviewer_id="forged-reviewer",
                approved_at=NOW + timedelta(minutes=1),
            ),
        )
        other_review = evaluate_google_place_identity_candidates(
            review.request,
            response(first, second),
            completed_at=NOW + timedelta(seconds=1),
        )
        forged_grant = self.issue_grant(
            other_review,
            selected.candidate.candidate_id,
            approved_at=NOW + timedelta(minutes=1),
        )
        self.assert_contract_error(
            "EVIDENCE_BINDING_MISMATCH",
            lambda: self.finalize_review(
                review,
                forged_grant,
            ),
        )

    def test_existing_identity_change_always_requires_review(self) -> None:
        initial_request = self.build_request()
        initial_review = evaluate_google_place_identity_candidates(
            initial_request,
            response(candidate()),
            completed_at=NOW,
        )
        authorized = self.finalize_review(initial_review)
        merged = merge_provider_result(
            EvidenceLedger(self.policies),
            authorized,
            purge_now=NOW + timedelta(minutes=1),
        )
        snapshot = EvidenceSnapshot.from_ledger(
            merged.ledger,
            evaluation_at=NOW + timedelta(minutes=1),
            purge_now=NOW + timedelta(minutes=1),
        )
        stale_refresh_request = (
            build_google_place_identity_refresh_request(
                snapshot,
                LOCATION_ID,
            )
        )
        request = self.build_request(snapshot=snapshot)
        review = evaluate_google_place_identity_candidates(
            request,
            response(candidate(place_id=OTHER_PLACE_ID)),
            completed_at=NOW + timedelta(minutes=2),
        )

        self.assertIs(
            PlaceIdentityReviewStatus.REVIEW_REQUIRED,
            review.status,
        )
        self.assert_contract_error(
            "PENDING_REVIEW",
            lambda: self.finalize_review(
                review,
                current_snapshot=snapshot,
                promotion_at=NOW + timedelta(minutes=3),
            ),
        )
        selected = review.eligible_assessments[0]
        grant = self.issue_grant(
            review,
            selected.candidate.candidate_id,
            approved_at=NOW + timedelta(minutes=3),
        )
        rebound_authorized = self.finalize_review(
            review,
            grant,
            current_snapshot=snapshot,
            promotion_at=NOW + timedelta(minutes=3),
        )
        self.assertEqual(
            {"provider_place_id": OTHER_PLACE_ID},
            observation_payload(rebound_authorized),
        )
        advanced = merge_provider_result(
            merged.ledger,
            rebound_authorized,
            purge_now=NOW + timedelta(minutes=3),
        )
        advanced_snapshot = EvidenceSnapshot.from_ledger(
            advanced.ledger,
            evaluation_at=NOW + timedelta(minutes=3),
            purge_now=NOW + timedelta(minutes=3),
        )
        self.assert_contract_error(
            "EVIDENCE_REVISION_CHANGED",
            lambda: self.finalize_review(
                review,
                grant,
                current_snapshot=advanced_snapshot,
                promotion_at=NOW + timedelta(minutes=4),
            ),
        )
        stale_refresh_authorized = finalize_google_place_identity_refresh(
            snapshot,
            stale_refresh_request,
            {"id": PLACE_ID},
            completed_at=NOW + timedelta(minutes=4),
        )
        stale_merge = merge_provider_result(
            advanced.ledger,
            stale_refresh_authorized,
            purge_now=NOW + timedelta(minutes=4),
        )
        self.assertFalse(stale_merge.changed)
        self.assertEqual(
            {"provider_place_id": OTHER_PLACE_ID},
            stale_merge.ledger.observations[0].value.payload,
        )
        self.assertIn(
            ProviderProblemCode.EVIDENCE_REVISION_CHANGED,
            {problem.code for problem in stale_merge.problems},
        )

    def test_expired_ready_and_granted_reviews_cannot_promote(self) -> None:
        ready = self.evaluate(response(candidate()))
        self.assert_contract_error(
            "PENDING_REVIEW",
            lambda: self.finalize_review(
                ready,
                promotion_at=NOW + timedelta(minutes=31),
            ),
        )

        ambiguous = self.evaluate(
            response(
                candidate(name="Busan Museum Main"),
                candidate(
                    place_id=OTHER_PLACE_ID,
                    name="Busan Museum Annex",
                ),
            )
        )
        selected = ambiguous.eligible_assessments[0]
        grant = self.issue_grant(
            ambiguous,
            selected.candidate.candidate_id,
        )
        self.assert_contract_error(
            "PENDING_REVIEW",
            lambda: self.finalize_review(
                ambiguous,
                grant,
                promotion_at=NOW + timedelta(minutes=31),
            ),
        )

    def test_current_snapshot_rechecks_newly_visible_existing_identity(
        self,
    ) -> None:
        seed_request = self.build_request()
        seed_review = evaluate_google_place_identity_candidates(
            seed_request,
            response(candidate()),
            completed_at=NOW + timedelta(minutes=5),
        )
        seed_authorized = self.finalize_review(
            seed_review,
            promotion_at=NOW + timedelta(minutes=5),
        )
        merged = merge_provider_result(
            EvidenceLedger(self.policies),
            seed_authorized,
            purge_now=NOW + timedelta(minutes=5),
        )
        early_snapshot = EvidenceSnapshot.from_ledger(
            merged.ledger,
            evaluation_at=NOW,
            purge_now=NOW + timedelta(minutes=5),
        )
        request = self.build_request(snapshot=early_snapshot)
        review = evaluate_google_place_identity_candidates(
            request,
            response(candidate(place_id=OTHER_PLACE_ID)),
            completed_at=NOW + timedelta(minutes=6),
        )
        self.assertIs(PlaceIdentityReviewStatus.READY, review.status)

        current_snapshot = EvidenceSnapshot.from_ledger(
            merged.ledger,
            evaluation_at=NOW + timedelta(minutes=6),
            purge_now=NOW + timedelta(minutes=6),
        )
        self.assert_contract_error(
            "EVIDENCE_REVISION_CHANGED",
            lambda: self.finalize_review(
                review,
                current_snapshot=current_snapshot,
                promotion_at=NOW + timedelta(minutes=7),
            ),
        )

    def test_name_token_boundaries_do_not_auto_match(self) -> None:
        review = self.evaluate(
            response(candidate(name="BusanMuseum")),
        )

        self.assertIs(
            PlaceIdentityReviewStatus.REVIEW_REQUIRED,
            review.status,
        )
        self.assertFalse(review.assessments[0].exact_name_match)
        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: self.build_request(expected_name="---"),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: self.evaluate(response(candidate(name="---"))),
        )

    def test_hard_country_locality_type_and_radius_mismatches_cannot_be_granted(
        self,
    ) -> None:
        cases = (
            (
                PlaceCandidateRejection.COUNTRY_MISMATCH,
                candidate(
                    country_code="JP",
                    country_name="Japan",
                ),
            ),
            (
                PlaceCandidateRejection.LOCALITY_MISMATCH,
                candidate(locality="Seoul"),
            ),
            (
                PlaceCandidateRejection.TYPE_MISMATCH,
                candidate(
                    primary_type="restaurant",
                    types=["restaurant", "food", "museum"],
                ),
            ),
            (
                PlaceCandidateRejection.OUTSIDE_GEOGRAPHIC_BOUNDARY,
                candidate(latitude=37.5665, longitude=126.9780),
            ),
        )

        for rejection, raw_candidate in cases:
            with self.subTest(rejection=rejection.value):
                review = self.evaluate(response(raw_candidate))
                self.assertIs(
                    PlaceIdentityReviewStatus.FAILED,
                    review.status,
                )
                assessment = review.assessments[0]
                self.assertIn(rejection, assessment.rejection_codes)
                self.assert_contract_error(
                    "OUT_OF_SCOPE_RESULT",
                    lambda review=review, assessment=assessment: (
                        self.issue_grant(
                            review,
                            assessment.candidate.candidate_id,
                        )
                    ),
                )

    def test_truncated_results_never_auto_promote(self) -> None:
        raw_response = response(candidate())
        raw_response["nextPageToken"] = "EPHEMERAL_PAGE_TOKEN_SENTINEL"

        review = self.evaluate(raw_response)

        self.assertTrue(review.results_truncated)
        self.assertIs(
            PlaceIdentityReviewStatus.REVIEW_REQUIRED,
            review.status,
        )
        self.assertIsNone(review.recommended_candidate_id)
        self.assert_contract_error(
            "PENDING_REVIEW",
            lambda: self.finalize_review(review),
        )
        durable_safe = json.dumps(
            review.to_binding_dict(),
            ensure_ascii=False,
        )
        self.assertNotIn("EPHEMERAL_PAGE_TOKEN_SENTINEL", durable_safe)

    def test_identity_request_rejects_mismatched_intent_scope(self) -> None:
        request = self.build_request()
        provider_request = request.provider_request
        changed_scope = tuple(
            (
                name,
                "Different Museum" if name == "expected_name" else value,
            )
            for name, value in provider_request.query_scope
        )
        forged = ProviderRequest(
            provider_id=provider_request.provider_id,
            adapter_id=provider_request.adapter_id,
            adapter_version=provider_request.adapter_version,
            operation=provider_request.operation,
            fact_keys=provider_request.fact_keys,
            policy_id=provider_request.policy_id,
            policy_digest=provider_request.policy_digest,
            query_scope=changed_scope,
        )

        self.assert_contract_error(
            "EVIDENCE_BINDING_MISMATCH",
            lambda: PlaceIdentityRequest(
                intent=request.intent,
                snapshot=request.snapshot,
                provider_request=forged,
                policy_registry_revision=request.policy_registry_revision,
                field_mask=request.field_mask,
            ),
        )

    def test_candidate_set_digest_is_permutation_stable(self) -> None:
        first = candidate(
            place_id=PLACE_ID,
            name="Busan Museum Main Hall",
        )
        second = candidate(
            place_id=OTHER_PLACE_ID,
            name="Busan Museum Annex",
            latitude=35.14,
            longitude=129.09,
        )
        request = self.build_request()

        forward = evaluate_google_place_identity_candidates(
            request,
            response(first, second),
            completed_at=NOW,
        )
        reverse = evaluate_google_place_identity_candidates(
            request,
            response(second, first),
            completed_at=NOW,
        )

        self.assertEqual(
            forward.candidate_set_digest,
            reverse.candidate_set_digest,
        )
        self.assertEqual(forward.review_id, reverse.review_id)
        self.assertEqual(
            [item.assessment_id for item in forward.assessments],
            [item.assessment_id for item in reverse.assessments],
        )

    def test_locality_collision_digest_is_hash_seed_stable(self) -> None:
        script = """
from trip_planner.places_identity import PlaceIdentityCandidate
candidate = PlaceIdentityCandidate(
    provider_place_id="ChIJ-locality-order",
    display_name="Busan Museum",
    formatted_address="Busan, South Korea",
    latitude=35.1379,
    longitude=129.0915,
    primary_type="museum",
    types=("museum",),
    country_code="KR",
    locality_names=("Busan", "BUSAN", "Busan."),
)
print(candidate.candidate_id)
"""
        candidate_ids = set()
        repository_root = Path(__file__).resolve().parents[1]
        for seed in ("1", "2", "3", "11", "37"):
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=repository_root,
                env={**os.environ, "PYTHONHASHSEED": seed},
                check=True,
                capture_output=True,
                text=True,
            )
            candidate_ids.add(completed.stdout.strip())

        self.assertEqual(1, len(candidate_ids))

    def test_empty_duplicate_oversized_malformed_and_extra_fields_fail_closed(
        self,
    ) -> None:
        empty = self.evaluate({})
        self.assertIs(PlaceIdentityReviewStatus.FAILED, empty.status)
        self.assert_contract_error(
            "OUT_OF_SCOPE_RESULT",
            lambda: self.finalize_review(empty),
        )

        duplicate = response(candidate(), candidate())
        malformed = response(candidate())
        del malformed["places"][0]["location"]
        oversized = response(
            *(
                candidate(
                    place_id=f"ChIJ-place-{index}",
                    name=f"Busan Museum {index}",
                )
                for index in range(6)
            )
        )
        restricted = response(
            candidate(
                name=SENTINEL_NAME,
                formatted_address=SENTINEL_ADDRESS,
            )
        )
        restricted["places"][0]["reviews"] = [
            {"text": "RESTRICTED_REVIEW_SENTINEL"}
        ]
        bad_language = response(candidate())
        bad_language["places"][0]["displayName"]["languageCode"] = 7
        oversized_component_types = response(candidate())
        oversized_component_types["places"][0]["addressComponents"][0][
            "types"
        ] = ["locality"] * 17

        for label, raw_response in (
            ("duplicate", duplicate),
            ("malformed", malformed),
            ("oversized", oversized),
            ("restricted_extra", restricted),
            ("bad_language", bad_language),
            ("oversized_component_types", oversized_component_types),
        ):
            with self.subTest(label=label):
                error = self.assert_contract_error(
                    "INVALID_PROVIDER_RESPONSE",
                    lambda raw_response=raw_response: self.evaluate(
                        raw_response
                    ),
                )
                self.assertNotIn("RESTRICTED_REVIEW_SENTINEL", repr(error))
                self.assertNotIn(SENTINEL_ADDRESS, repr(error))

    def test_final_result_is_id_only_static_and_fresh_for_one_calendar_year(
        self,
    ) -> None:
        raw = candidate(
            name=SENTINEL_NAME,
            formatted_address=SENTINEL_ADDRESS,
        )
        review = self.evaluate(
            response(raw),
            expected_name=SENTINEL_NAME,
        )
        authorized = self.finalize_review(review)
        result = raw_result(authorized)

        self.assertIs(ProviderResultStatus.SUCCESS, result.status)
        self.assertEqual(1, result.attempts_used)
        self.assertEqual(1, len(result.observations))
        observation = result.observations[0]
        self.assertEqual(
            {"provider_place_id": PLACE_ID},
            observation.value.payload,
        )
        self.assertEqual(NOW, observation.retrieved_at)
        self.assertEqual(
            NOW.replace(year=NOW.year + 1),
            observation.valid_until,
        )
        self.assertIsNone(observation.purge_at)
        self.assertEqual(1.0, observation.confidence)
        self.assertEqual(
            (("Google Maps", None),),
            observation.provenance.attributions,
        )
        self.assertEqual(
            PLACE_ID,
            observation.provenance.provider_record_id,
        )
        self.assertIsNone(observation.provenance.response_id)
        self.assertIsNone(observation.provenance.source_uri)

        safe = json.dumps(
            {
                "review_repr": repr(review),
                "review_binding": review.to_binding_dict(),
                "authorized_repr": repr(authorized),
                "authorized_binding": authorized.to_binding_dict(),
                "result": result.to_dict(),
            },
            ensure_ascii=False,
        )
        self.assertNotIn(SENTINEL_NAME, safe)
        self.assertNotIn(SENTINEL_ADDRESS, safe)
        self.assertNotIn(SENTINEL_QUERY, safe)
        self.assertNotIn("35.1379", safe)
        self.assertNotIn('"museum"', safe)

    def test_authorized_identity_round_trip_persists_no_review_content(
        self,
    ) -> None:
        raw = candidate(
            name=SENTINEL_NAME,
            formatted_address=SENTINEL_ADDRESS,
            latitude=35.123456,
            longitude=129.654321,
        )
        request = self.build_request(
            expected_name=SENTINEL_NAME,
            latitude=35.123456,
            longitude=129.654321,
            radius_m=1_000,
        )
        review = evaluate_google_place_identity_candidates(
            request,
            response(raw),
            completed_at=NOW,
        )
        authorized = self.finalize_review(review)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample-trip" / "data").mkdir(parents=True)
            store = EvidenceStore(
                root,
                "sample-trip",
                "trip-sample",
                self.policies,
                clock=lambda: NOW + timedelta(minutes=1),
            )
            merged = store.merge(authorized)
            self.assertTrue(merged.success, merged.problems)
            durable = store.cache_path.read_text(encoding="utf-8")

            self.assertIn(PLACE_ID, durable)
            for restricted_value in (
                SENTINEL_NAME,
                SENTINEL_ADDRESS,
                SENTINEL_QUERY,
                "35.123456",
                "129.654321",
                '"museum"',
                "tourist_attraction",
            ):
                with self.subTest(restricted_value=restricted_value):
                    self.assertNotIn(restricted_value, durable)

            loaded = store.load()
            self.assertTrue(loaded.success, loaded.problems)
            self.assertEqual(1, len(loaded.ledger.observations))
            self.assertEqual(
                {"provider_place_id": PLACE_ID},
                loaded.ledger.observations[0].value.payload,
            )

    def test_id_only_refresh_accepts_same_id_and_rejects_changed_id(
        self,
    ) -> None:
        initial_request = self.build_request()
        initial_review = evaluate_google_place_identity_candidates(
            initial_request,
            response(candidate()),
            completed_at=NOW,
        )
        authorized = self.finalize_review(initial_review)
        merged = merge_provider_result(
            EvidenceLedger(self.policies),
            authorized,
            purge_now=NOW + timedelta(minutes=1),
        )
        snapshot = EvidenceSnapshot.from_ledger(
            merged.ledger,
            evaluation_at=NOW + timedelta(minutes=1),
            purge_now=NOW + timedelta(minutes=1),
        )
        request = build_google_place_identity_refresh_request(
            snapshot,
            LOCATION_ID,
        )
        scope = dict(request.query_scope)
        self.assertEqual("id", scope["field_mask"])
        self.assertEqual(PLACE_ID, scope["provider_place_id"])
        self.assertEqual(PLACE_ID, scope["basis_provider_place_id"])
        self.assertEqual(
            raw_result(authorized).observations[0].observation_id,
            scope["basis_observation_id"],
        )
        self.assertEqual(snapshot.snapshot_id, scope["basis_snapshot_id"])
        same_authorized = finalize_google_place_identity_refresh(
            snapshot,
            request,
            {"id": PLACE_ID},
            completed_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(
            {"provider_place_id": PLACE_ID},
            observation_payload(same_authorized),
        )
        refreshed = merge_provider_result(
            merged.ledger,
            same_authorized,
            purge_now=NOW + timedelta(minutes=2),
        )
        self.assertTrue(refreshed.changed)
        self.assertEqual(1, len(refreshed.promoted_observation_ids))
        self.assertNotIn(
            ProviderProblemCode.EVIDENCE_REVISION_CHANGED,
            {problem.code for problem in refreshed.problems},
        )

        forged_refresh = unreviewed_identity_result(
            request,
            OTHER_PLACE_ID,
            completed_at=NOW + timedelta(minutes=2),
        )
        self.assert_contract_error(
            "PENDING_REVIEW",
            lambda: authorize_provider_result(
                request,
                forged_refresh,
                self.policies,
            ),
        )
        self.assert_contract_error(
            "PENDING_REVIEW",
            lambda: finalize_google_place_identity_refresh(
                snapshot,
                request,
                {"id": OTHER_PLACE_ID},
                completed_at=NOW + timedelta(minutes=2),
            ),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_RESPONSE",
            lambda: finalize_google_place_identity_refresh(
                snapshot,
                request,
                {
                    "id": PLACE_ID,
                    "displayName": {
                        "text": "Restricted refresh payload"
                    },
                },
                completed_at=NOW + timedelta(minutes=2),
            ),
        )
        self.assert_contract_error(
            "PENDING_REVIEW",
            lambda: build_google_place_identity_refresh_request(
                self.empty_snapshot,
                LOCATION_ID,
            ),
        )

    def test_extract_endpoint_requires_fresh_identity_and_rejects_missing_or_stale(
        self,
    ) -> None:
        request = self.build_request()
        review = evaluate_google_place_identity_candidates(
            request,
            response(candidate()),
            completed_at=NOW,
        )
        authorized = self.finalize_review(review)
        result = raw_result(authorized)
        observation = result.observations[0]
        self.assert_contract_error(
            "UNTRUSTED_PROVENANCE",
            lambda: PlaceEndpointIdentity(
                location_id=LOCATION_ID,
                provider_id="google-places",
                provider_place_id=PLACE_ID,
                observation_id=observation.observation_id,
                value_digest=observation.value.value_digest,
                valid_until=observation.valid_until,
                snapshot_id=self.empty_snapshot.snapshot_id,
            ),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample-trip" / "data").mkdir(parents=True)
            store = EvidenceStore(
                root,
                "sample-trip",
                "trip-sample",
                self.policies,
                clock=lambda: NOW + timedelta(minutes=1),
            )
            merged = store.merge(authorized)
            self.assertTrue(merged.success, merged.problems)

            fresh_snapshot = merged.snapshot(
                evaluation_at=NOW + timedelta(days=1)
            )
            endpoint = extract_fresh_google_place_endpoint(
                fresh_snapshot,
                LOCATION_ID,
            )
            self.assertEqual(LOCATION_ID, endpoint.location_id)
            self.assertEqual("google-places", endpoint.provider_id)
            self.assertEqual(PLACE_ID, endpoint.provider_place_id)
            self.assertEqual(
                result.observations[0].observation_id,
                endpoint.observation_id,
            )
            endpoint_safe = json.dumps(
                {
                    "repr": repr(endpoint),
                    "binding": endpoint.to_binding_dict(),
                },
                ensure_ascii=False,
            )
            self.assertNotIn(PLACE_ID, endpoint_safe)
            self.assertNotIn(SENTINEL_QUERY, endpoint_safe)
            self.assertEqual(
                result.observations[0].value.value_digest,
                endpoint.value_digest,
            )

            self.assert_contract_error(
                "PENDING_REVIEW",
                lambda: extract_fresh_google_place_endpoint(
                    fresh_snapshot,
                    "location-missing",
                ),
            )

            stale_snapshot = merged.snapshot(
                evaluation_at=NOW.replace(year=NOW.year + 1)
            )
            self.assert_contract_error(
                "STALE_EVIDENCE",
                lambda: extract_fresh_google_place_endpoint(
                    stale_snapshot,
                    LOCATION_ID,
                ),
            )


if __name__ == "__main__":
    unittest.main()
