"""Offline contract tests for Phase 4.5B hotel discovery normalization."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone

import trip_planner.lodging_discovery as discovery_module
from trip_planner.lodging import IntentAuthority, LodgingKind, PriceBasis
from trip_planner.lodging_discovery import (
    LodgingDiscoveryProblemCode,
    LodgingDiscoveryRequest,
    LodgingDiscoveryResult,
    LodgingDiscoveryStatus,
    normalize_serpapi_hotel_discovery as _normalize_discovery,
)
from trip_planner.lodging_evidence import assess_lodging_evidence
from trip_planner.models import DecisionState, EvidenceState


QUERY = "private Kyoto stay token=query-secret"
NAME = "Private Ryokan sentinel"
ADDRESS = "Secret street 1 token=address-secret"
SEARCH_ID = "provider-search-secret"
BOOKING_TOKEN = "provider-booking-token-secret"
COMPLETED_AT = datetime(2026, 7, 29, 10, tzinfo=timezone.utc)


def normalize_serpapi_hotel_discovery(request_value, raw):
    return _normalize_discovery(
        request_value,
        raw,
        completed_at=COMPLETED_AT,
    )


def request(
    query: str = QUERY,
    *,
    currency: str = "JPY",
    currency_minor_unit: int = 0,
) -> LodgingDiscoveryRequest:
    return LodgingDiscoveryRequest(
        query=query,
        check_in=date(2026, 10, 1),
        check_out=date(2026, 10, 3),
        adults=2,
        children=0,
        rooms=1,
        currency=currency,
        currency_minor_unit=currency_minor_unit,
        region="jp",
        language="ja",
    )


def metadata(
    *,
    status: str = "Success",
    search_id: str = SEARCH_ID,
) -> dict[str, object]:
    return {"status": status, "id": search_id}


def response(*properties: object, status: str = "Success"):
    return {
        "search_metadata": metadata(status=status),
        "properties": list(properties),
    }


def property(
    name: str = NAME,
    latitude: float = 35.0,
    longitude: float = 135.0,
) -> dict[str, object]:
    return {
        "name": name,
        "gps_coordinates": {
            "latitude": latitude,
            "longitude": longitude,
        },
        "price": {
            "amount_minor": 12_000,
            "currency": "JPY",
            "minor_unit": 0,
            "basis": "nightly",
        },
        "booking_token": BOOKING_TOKEN,
    }


class LodgingDiscoveryTests(unittest.TestCase):
    def test_success_is_candidate_only_and_private(self) -> None:
        result = normalize_serpapi_hotel_discovery(
            request(),
            response(property()),
        )
        self.assertEqual(LodgingDiscoveryStatus.SUCCESS, result.status)
        self.assertEqual("success", result.metadata_status)
        self.assertIsNotNone(result.provider_search_ref)
        candidate = result.candidates[0]
        self.assertEqual(
            IntentAuthority.PROVIDER_DISCOVERED,
            candidate.authority,
        )
        self.assertEqual(DecisionState.CANDIDATE, candidate.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, candidate.evidence_state)
        self.assertEqual((), candidate.evidence_refs)
        self.assertEqual(12_000, candidate.draft.price_amount_minor)
        self.assertEqual(PriceBasis.NIGHTLY, candidate.draft.price_basis)

        safe = (
            repr(result)
            + repr(result.request)
            + json.dumps(result.to_dict(), sort_keys=True)
        )
        for secret in (
            "Kyoto",
            "query-secret",
            NAME,
            SEARCH_ID,
            BOOKING_TOKEN,
            "135.0",
            "12000",
        ):
            self.assertNotIn(secret, safe)

    def test_provider_error_empty_and_invalid_response_are_distinct(
        self,
    ) -> None:
        provider_error = normalize_serpapi_hotel_discovery(
            request(),
            {
                "search_metadata": metadata(status="Error"),
                "error": "account provider-error-secret",
                "properties": [],
            },
        )
        empty = normalize_serpapi_hotel_discovery(
            request(),
            response(),
        )
        malformed = normalize_serpapi_hotel_discovery(
            request(),
            {
                "search_metadata": metadata(),
                "properties": "not-list",
            },
        )
        self.assertEqual(
            LodgingDiscoveryStatus.PROVIDER_ERROR,
            provider_error.status,
        )
        self.assertEqual(LodgingDiscoveryStatus.EMPTY, empty.status)
        self.assertEqual(
            LodgingDiscoveryStatus.INVALID_RESPONSE,
            malformed.status,
        )
        self.assertEqual(
            LodgingDiscoveryProblemCode.EMPTY_SUCCESS,
            empty.problems[0].code,
        )
        self.assertNotIn(
            "provider-error-secret",
            json.dumps(provider_error.to_dict()),
        )

    def test_missing_or_unknown_metadata_status_fails_closed(self) -> None:
        missing = normalize_serpapi_hotel_discovery(
            request(),
            {"properties": [property()]},
        )
        processing = normalize_serpapi_hotel_discovery(
            request(),
            response(property(), status="Processing"),
        )
        self.assertEqual(
            LodgingDiscoveryStatus.INVALID_RESPONSE,
            missing.status,
        )
        self.assertEqual(
            LodgingDiscoveryStatus.INVALID_RESPONSE,
            processing.status,
        )
        self.assertEqual("processing", processing.metadata_status)

        private_status = normalize_serpapi_hotel_discovery(
            request(),
            response(
                property(),
                status="PRIVATE-STATUS-SENTINEL-9",
            ),
        )
        self.assertEqual(
            LodgingDiscoveryStatus.INVALID_RESPONSE,
            private_status.status,
        )
        self.assertEqual("unknown", private_status.metadata_status)
        self.assertNotIn(
            "private_status_sentinel",
            json.dumps(private_status.to_dict()),
        )

    def test_partial_keeps_candidate_but_marks_price_and_location_gaps(
        self,
    ) -> None:
        no_location = {"name": "No address"}
        bad_price = property("Priceless")
        bad_price["price"] = {
            "amount_minor": 12,
            "currency": "USD",
            "minor_unit": 2,
            "basis": "nightly",
        }
        result = normalize_serpapi_hotel_discovery(
            request(),
            response(no_location, no_location, bad_price),
        )
        self.assertEqual(LodgingDiscoveryStatus.PARTIAL, result.status)
        self.assertEqual(1, len(result.candidates))
        self.assertIsNone(
            result.candidates[0].draft.price_amount_minor
        )
        codes = {problem.code for problem in result.problems}
        self.assertIn(
            LodgingDiscoveryProblemCode.PROPERTY_NO_LOCATION,
            codes,
        )
        self.assertIn(
            LodgingDiscoveryProblemCode.PROPERTY_PRICE_UNUSABLE,
            codes,
        )
        self.assertEqual(
            1,
            sum(
                problem.code
                is LodgingDiscoveryProblemCode.PROPERTY_NO_LOCATION
                for problem in result.problems
            ),
        )

    def test_native_serpapi_rates_convert_using_explicit_minor_unit(
        self,
    ) -> None:
        item = property()
        item.pop("price")
        item["total_rate"] = {"extracted_lowest": "1234.50"}
        result = normalize_serpapi_hotel_discovery(
            request(currency="TWD", currency_minor_unit=2),
            response(item),
        )
        candidate = result.candidates[0]
        self.assertEqual(123_450, candidate.draft.price_amount_minor)
        self.assertEqual(PriceBasis.TOTAL, candidate.draft.price_basis)
        self.assertTrue(candidate.draft.price_is_estimate)
        self.assertNotIn(
            "123450",
            json.dumps(result.to_dict(), sort_keys=True),
        )

    def test_unrepresentable_provider_price_stays_unknown(self) -> None:
        item = property()
        item.pop("price")
        item["rate_per_night"] = {"extracted_lowest": "12.345"}
        result = normalize_serpapi_hotel_discovery(
            request(currency="TWD", currency_minor_unit=2),
            response(item),
        )
        self.assertEqual(LodgingDiscoveryStatus.PARTIAL, result.status)
        self.assertIsNone(
            result.candidates[0].draft.price_amount_minor
        )
        self.assertIn(
            LodgingDiscoveryProblemCode.PROPERTY_PRICE_UNUSABLE,
            {problem.code for problem in result.problems},
        )

    def test_dedupe_and_permutation_are_deterministic(self) -> None:
        first = property("A")
        duplicate = property("A duplicate")
        second = property("B", 36, 136)
        one = normalize_serpapi_hotel_discovery(
            request(),
            response(first, duplicate, duplicate, second),
        )
        two = normalize_serpapi_hotel_discovery(
            request(),
            response(second, duplicate, first),
        )
        self.assertEqual(
            [candidate.candidate_id for candidate in one.candidates],
            [candidate.candidate_id for candidate in two.candidates],
        )
        self.assertEqual(
            [problem.to_dict() for problem in one.problems],
            [problem.to_dict() for problem in two.problems],
        )
        self.assertEqual(one.diagnostic_ref, two.diagnostic_ref)
        self.assertEqual(LodgingDiscoveryStatus.PARTIAL, one.status)

    def test_property_bound_is_checked_before_normalization(self) -> None:
        too_many = normalize_serpapi_hotel_discovery(
            request(),
            response(
                *(
                    property(str(index), 1 + index / 1000, 2)
                    for index in range(257)
                )
            ),
        )
        self.assertEqual(
            LodgingDiscoveryStatus.INVALID_RESPONSE,
            too_many.status,
        )
        self.assertEqual(
            LodgingDiscoveryProblemCode.PROPERTY_LIMIT_EXCEEDED,
            too_many.problems[0].code,
        )

    def test_address_and_provider_neutral_kind_are_supported(self) -> None:
        result = normalize_serpapi_hotel_discovery(
            request(),
            response(
                {
                    "name": "Address stay",
                    "address": ADDRESS,
                    "type": "Vacation rental apartment",
                }
            ),
        )
        self.assertEqual(LodgingDiscoveryStatus.SUCCESS, result.status)
        candidate = result.candidates[0]
        self.assertEqual(
            "unresolved",
            candidate.draft.location.precision.value,
        )
        self.assertEqual(
            LodgingKind.SHORT_TERM_RENTAL,
            candidate.draft.kind,
        )
        self.assertNotIn(ADDRESS, json.dumps(result.to_dict()))

    def test_sponsored_only_is_empty_success_not_a_candidate(self) -> None:
        item = property()
        item["sponsored"] = True
        result = normalize_serpapi_hotel_discovery(
            request(),
            response(item),
        )
        self.assertEqual(LodgingDiscoveryStatus.EMPTY, result.status)
        self.assertEqual((), result.candidates)
        self.assertEqual(
            LodgingDiscoveryProblemCode.EMPTY_SUCCESS,
            result.problems[0].code,
        )

    def test_malformed_nested_values_do_not_escape_or_crash(self) -> None:
        item = {
            "name": "Malformed location",
            "gps_coordinates": {
                "latitude": object(),
                "longitude": object(),
            },
            "provider_token": "malformed-provider-secret",
        }
        result = normalize_serpapi_hotel_discovery(
            request(),
            response(item),
        )
        self.assertEqual(
            LodgingDiscoveryStatus.INVALID_RESPONSE,
            result.status,
        )
        self.assertNotIn(
            "malformed-provider-secret",
            repr(result) + json.dumps(result.to_dict()),
        )

    def test_provider_identifiers_and_property_shapes_are_bounded(
        self,
    ) -> None:
        large_search_id = "private-search-id-" + "x" * 20_000
        search_result = normalize_serpapi_hotel_discovery(
            request(),
            {
                "search_metadata": metadata(
                    search_id=large_search_id,
                ),
                "properties": [property()],
            },
        )
        self.assertEqual(
            LodgingDiscoveryStatus.INVALID_RESPONSE,
            search_result.status,
        )
        self.assertNotIn(
            "private-search-id",
            json.dumps(search_result.to_dict()),
        )

        large_latitude = "private-gps-" + "y" * 20_000
        gps_result = normalize_serpapi_hotel_discovery(
            request(),
            response(
                {
                    "name": "Malformed GPS",
                    "gps_coordinates": {
                        "latitude": large_latitude,
                        "longitude": 135.0,
                    },
                }
            ),
        )
        self.assertEqual(
            LodgingDiscoveryStatus.INVALID_RESPONSE,
            gps_result.status,
        )
        self.assertNotIn(
            "private-gps",
            json.dumps(gps_result.to_dict()),
        )

        oversized_property = {
            "name": "Oversized mapping",
            **{f"ignored_{index}": index for index in range(129)},
        }
        property_result = normalize_serpapi_hotel_discovery(
            request(),
            response(oversized_property),
        )
        self.assertEqual(
            LodgingDiscoveryStatus.INVALID_RESPONSE,
            property_result.status,
        )

        huge_scalars = property("Huge scalars")
        huge_scalars.pop("price")
        huge_scalars["type"] = "private-kind-" + "k" * 20_000
        huge_scalars["rate_per_night"] = {
            "extracted_lowest": "9" * 20_000,
        }
        scalar_result = normalize_serpapi_hotel_discovery(
            request(),
            response(huge_scalars),
        )
        self.assertEqual(
            LodgingDiscoveryStatus.PARTIAL,
            scalar_result.status,
        )
        self.assertEqual(
            LodgingKind.HOTEL,
            scalar_result.candidates[0].draft.kind,
        )
        self.assertIsNone(
            scalar_result.candidates[0].draft.price_amount_minor
        )
        self.assertNotIn(
            "private-kind",
            json.dumps(scalar_result.to_dict()),
        )

    def test_result_is_non_provenance_and_invalid_shapes_are_rejected(
        self,
    ) -> None:
        direct = LodgingDiscoveryResult(
            request(),
            response(property()),
            completed_at=COMPLETED_AT,
        )
        self.assertEqual(LodgingDiscoveryStatus.SUCCESS, direct.status)
        self.assertEqual(
            "untrusted_caller_supplied",
            direct.provenance,
        )
        self.assertFalse(direct.supports_authoritative_use)
        self.assertFalse(
            direct.to_dict()["supports_authoritative_use"]
        )
        self.assertNotIn("result_id", direct.to_dict())

        with self.assertRaises(TypeError):
            LodgingDiscoveryResult(
                request=request(),
                status=LodgingDiscoveryStatus.SUCCESS,
                completed_at=COMPLETED_AT,
                candidates=direct.candidates,
                metadata_status="success",
            )

        for status in LodgingDiscoveryStatus:
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    discovery_module._LodgingDiscoveryProjection(
                        request=request(),
                        status=status,
                        completed_at=COMPLETED_AT,
                        metadata_status="success",
                    )

        # Python reflection can fabricate any DTO instance.  This object still
        # carries no provenance bit, and the evidence assessor rejects the
        # result type instead of accepting its status or diagnostic digest.
        forged = object.__new__(LodgingDiscoveryResult)
        object.__setattr__(forged, "_projection", direct._projection)
        self.assertFalse(forged.supports_authoritative_use)
        with self.assertRaises(TypeError):
            assess_lodging_evidence(
                candidates=(forged,),  # type: ignore[arg-type]
                snapshot=object(),  # type: ignore[arg-type]
            )

    def test_request_rejects_datetime_and_safe_view_hides_query(self) -> None:
        with self.assertRaises(TypeError):
            LodgingDiscoveryRequest(
                query=QUERY,
                check_in=datetime(2026, 10, 1, tzinfo=timezone.utc),
                check_out=date(2026, 10, 3),
                adults=2,
                children=0,
                rooms=1,
                currency="JPY",
                currency_minor_unit=0,
                region="JP",
                language="ja",
            )
        safe = repr(request()) + json.dumps(request().to_dict())
        self.assertNotIn(QUERY, safe)

    def test_raw_input_must_be_mapping(self) -> None:
        invalid = normalize_serpapi_hotel_discovery(
            request(),
            [],  # type: ignore[arg-type]
        )
        self.assertEqual(
            LodgingDiscoveryStatus.INVALID_RESPONSE,
            invalid.status,
        )


if __name__ == "__main__":
    unittest.main()
