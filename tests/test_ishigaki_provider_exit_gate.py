"""Offline tests for the bounded, non-persistent Ishigaki provider gate."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trip_planner.routes import GOOGLE_ROUTES_FIELD_MASK, GoogleRoutesHttpResponse


UTC = timezone.utc
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "ishigaki_provider_exit_gate.py"
)
SPEC = importlib.util.spec_from_file_location(
    "ishigaki_provider_exit_gate_test_module",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def _candidate_response(
    *,
    place_id: str,
    name: str,
    latitude: float,
    longitude: float,
    next_page_token: str | None = None,
) -> GoogleRoutesHttpResponse:
    body: dict[str, object] = {
        "places": [
            {
                "id": place_id,
                "displayName": {"text": name, "languageCode": "en"},
                "formattedAddress": "Sentinel address, Ishigaki, Japan",
                "location": {"latitude": latitude, "longitude": longitude},
                "primaryType": "tourist_attraction",
                "types": ["tourist_attraction", "point_of_interest"],
                "addressComponents": [
                    {
                        "longText": "Ishigaki",
                        "shortText": "Ishigaki",
                        "types": ["locality", "political"],
                        "languageCode": "en",
                    },
                    {
                        "longText": "Japan",
                        "shortText": "JP",
                        "types": ["country", "political"],
                        "languageCode": "en",
                    },
                ],
            }
        ]
    }
    if next_page_token is not None:
        body["nextPageToken"] = next_page_token
    return GoogleRoutesHttpResponse(
        status_code=200,
        body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
    )


def _route_response() -> GoogleRoutesHttpResponse:
    return GoogleRoutesHttpResponse(
        status_code=200,
        body=json.dumps(
            {
                "routes": [
                    {
                        "duration": "600s",
                        "staticDuration": "540s",
                        "distanceMeters": 2_000,
                    }
                ]
            },
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def _route_auth_failure_response() -> GoogleRoutesHttpResponse:
    return GoogleRoutesHttpResponse(
        status_code=403,
        body=b'{"error":{"status":"PERMISSION_DENIED"}}',
    )


def _ambiguous_origin_response() -> GoogleRoutesHttpResponse:
    first = json.loads(
        _candidate_response(
            place_id="sentinel-origin-id-a",
            name="Kabira Bay Glass Boat",
            latitude=24.457,
            longitude=124.144,
        ).body
    )["places"][0]
    second = json.loads(
        _candidate_response(
            place_id="sentinel-origin-id-b",
            name="Kabira Bay Glass Boat",
            latitude=24.458,
            longitude=124.145,
        ).body
    )["places"][0]
    return GoogleRoutesHttpResponse(
        status_code=200,
        body=json.dumps({"places": [first, second]}, separators=(",", ":")).encode(
            "utf-8"
        ),
    )


class CannedTransport:
    def __init__(
        self,
        identity_responses: list[GoogleRoutesHttpResponse],
        route_responses: list[GoogleRoutesHttpResponse],
        *,
        after_identity_call=None,
    ) -> None:
        self.identity_responses = list(identity_responses)
        self.route_responses = list(route_responses)
        self.after_identity_call = after_identity_call
        self.identity_requests = []
        self.route_requests = []
        self.diagnostic_route_requests = []

    def search_place_identity(
        self,
        request,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        self.identity_requests.append(
            (request, connect_timeout_s, read_timeout_s)
        )
        if self.after_identity_call is not None:
            callback = self.after_identity_call
            self.after_identity_call = None
            callback()
        if not self.identity_responses:
            raise AssertionError("unexpected identity provider request")
        return self.identity_responses.pop(0)

    def send(
        self,
        request,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        self.route_requests.append((request, connect_timeout_s, read_timeout_s))
        if not self.route_responses:
            raise AssertionError("unexpected route provider request")
        return self.route_responses.pop(0)

    def send_route_diagnostic(
        self,
        request,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        self.diagnostic_route_requests.append(
            (request, connect_timeout_s, read_timeout_s)
        )
        if not self.route_responses:
            raise AssertionError("unexpected diagnostic route provider request")
        return self.route_responses.pop(0)


class IshigakiProviderExitGateTests(unittest.TestCase):
    def _write_trip(self, root: Path) -> Path:
        data_dir = root / "ishigaki-2026-10" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "trip.json").write_text(
            json.dumps({"slug": "ishigaki-2026-10"}), encoding="utf-8"
        )
        queries = {
            "Kabira Bay Glass Boat": "Kabira Bay Glass Boat, Ishigaki, Okinawa, Japan",
            "Yonehara Beach": "Yonehara Beach, Ishigaki, Okinawa, Japan",
        }
        (data_dir / "place_candidates.json").write_text(
            json.dumps(
                {
                    "candidates": [
                        {"name": name, "maps_query": query}
                        for name, query in queries.items()
                    ]
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        (data_dir / "itinerary.json").write_text(
            json.dumps(
                {
                    "days": [
                        {
                            "date": "2026-10-04",
                            "places": [
                                {
                                    "maps_query": queries["Kabira Bay Glass Boat"],
                                    "time": "10:15",
                                    "lat": 24.457,
                                    "lng": 124.144,
                                },
                                {
                                    "maps_query": queries["Yonehara Beach"],
                                    "time": "12:00",
                                    "lat": 24.443,
                                    "lng": 124.191,
                                },
                            ],
                        }
                    ]
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return data_dir.parent

    def _tree_digest(self, trip_dir: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(trip_dir.rglob("*")):
            if not path.is_file():
                continue
            digest.update(path.relative_to(trip_dir).as_posix().encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def test_success_uses_two_identity_calls_then_one_memory_only_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            before = self._tree_digest(trip_dir)
            transport = CannedTransport(
                [
                    _candidate_response(
                        place_id="sentinel-origin-id",
                        name="Kabira Bay Glass Boat",
                        latitude=24.457,
                        longitude=124.144,
                    ),
                    _candidate_response(
                        place_id="sentinel-destination-id",
                        name="Yonehara Beach",
                        latitude=24.443,
                        longitude=124.191,
                    ),
                ],
                [_route_response()],
            )

            result = gate.run_ishigaki_provider_exit_gate(
                trip_dir,
                transport=transport,
                clock=lambda: NOW,
            )

            self.assertEqual("completed_memory_only", result["status"])
            self.assertEqual(
                {"identity": 2, "route": 1, "total": 3},
                result["attempts"],
            )
            self.assertEqual(
                {"requested": 2, "verified_in_memory": 2},
                result["identities"],
            )
            self.assertEqual(
                {
                    "requested": 1,
                    "checked": True,
                    "persistence": "discarded_memory_only",
                },
                result["route"],
            )
            self.assertTrue(result["trip_files_modified"] is False)
            self.assertEqual(2, len(transport.identity_requests))
            self.assertEqual(1, len(transport.route_requests))
            self.assertEqual(before, self._tree_digest(trip_dir))

            route_request = transport.route_requests[0][0]
            self.assertEqual(GOOGLE_ROUTES_FIELD_MASK, route_request.field_mask)
            self.assertEqual(
                GOOGLE_ROUTES_FIELD_MASK,
                route_request.headers["X-Goog-FieldMask"],
            )
            self.assertEqual(
                {
                    "computeAlternativeRoutes": False,
                    "departureTime": "2026-10-04T01:15:00Z",
                    "destination": {"placeId": "sentinel-destination-id"},
                    "origin": {"placeId": "sentinel-origin-id"},
                    "travelMode": "DRIVE",
                },
                json.loads(route_request.body),
            )

            request = transport.identity_requests[0][0]
            body = json.loads(request.body)
            self.assertIn("nextPageToken", request.field_mask)
            self.assertIn("nextPageToken", request.headers["X-Goog-FieldMask"])
            self.assertEqual(5, body["pageSize"])
            self.assertEqual(
                {"languageCode", "locationBias", "pageSize", "regionCode", "textQuery"},
                set(body),
            )

    def test_pagination_token_stops_before_route_and_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            before = self._tree_digest(trip_dir)
            transport = CannedTransport(
                [
                    _candidate_response(
                        place_id="sentinel-origin-id",
                        name="Kabira Bay Glass Boat",
                        latitude=24.457,
                        longitude=124.144,
                        next_page_token="ephemeral-sentinel-page-token",
                    )
                ],
                [],
            )

            result = gate.run_ishigaki_provider_exit_gate(
                trip_dir,
                transport=transport,
                clock=lambda: NOW,
            )

            self.assertEqual("identity_review_required", result["status"])
            self.assertEqual(1, len(transport.identity_requests))
            self.assertEqual([], transport.route_requests)
            self.assertEqual(before, self._tree_digest(trip_dir))

            safe = json.dumps(result, sort_keys=True)
            self.assertNotIn("ephemeral-sentinel-page-token", safe)

    def test_route_failure_exposes_only_safe_failure_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            before = self._tree_digest(trip_dir)
            transport = CannedTransport(
                [
                    _candidate_response(
                        place_id="sentinel-origin-id",
                        name="Kabira Bay Glass Boat",
                        latitude=24.457,
                        longitude=124.144,
                    ),
                    _candidate_response(
                        place_id="sentinel-destination-id",
                        name="Yonehara Beach",
                        latitude=24.443,
                        longitude=124.191,
                    ),
                ],
                [_route_auth_failure_response()],
            )

            result = gate.run_ishigaki_provider_exit_gate(
                trip_dir,
                transport=transport,
                clock=lambda: NOW,
            )

            self.assertEqual("route_unavailable", result["status"])
            self.assertEqual(
                {
                    "requested": 1,
                    "checked": False,
                    "persistence": "discarded_memory_only",
                    "failure_class": "auth_failed",
                },
                result["route"],
            )
            self.assertEqual(2, len(transport.identity_requests))
            self.assertEqual(1, len(transport.route_requests))
            self.assertEqual(before, self._tree_digest(trip_dir))
            safe = json.dumps(result, sort_keys=True)
            self.assertNotIn("PERMISSION_DENIED", safe)
            self.assertNotIn("sentinel-origin-id", safe)
            self.assertNotIn("sentinel-destination-id", safe)

    def test_minimal_route_diagnostic_uses_reduced_mask_and_discards_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            before = self._tree_digest(trip_dir)
            review = gate.run_ishigaki_origin_candidate_review(
                trip_dir,
                transport=CannedTransport([_ambiguous_origin_response()], []),
                clock=lambda: NOW,
            )
            binding = review["review"]["selection_binding_v2"]
            opaque_body = b"diagnostic-route-response-sentinel"
            transport = CannedTransport(
                [
                    _ambiguous_origin_response(),
                    _candidate_response(
                        place_id="sentinel-destination-id",
                        name="Yonehara Beach",
                        latitude=24.443,
                        longitude=124.191,
                    ),
                ],
                [GoogleRoutesHttpResponse(status_code=200, body=opaque_body)],
            )

            result = gate.run_ishigaki_minimal_route_diagnostic(
                trip_dir,
                transport=transport,
                origin_choice="B",
                origin_selection_binding_v2=binding,
                clock=lambda: NOW + timedelta(minutes=1),
            )

            self.assertEqual("minimal_route_diagnostic_completed", result["status"])
            self.assertEqual(
                {"identity": 2, "route": 1, "total": 3},
                result["attempts"],
            )
            self.assertEqual(
                {
                    "requested": 1,
                    "request_accepted": True,
                    "diagnostic": "minimal_response_mask",
                    "persistence": "discarded_memory_only",
                },
                result["route"],
            )
            self.assertEqual(2, len(transport.identity_requests))
            self.assertEqual([], transport.route_requests)
            self.assertEqual(1, len(transport.diagnostic_route_requests))
            self.assertEqual(before, self._tree_digest(trip_dir))

            request = transport.diagnostic_route_requests[0][0]
            self.assertEqual(
                "routes.distanceMeters,routes.duration",
                request.field_mask,
            )
            self.assertEqual(
                "routes.distanceMeters,routes.duration",
                request.headers["X-Goog-FieldMask"],
            )
            self.assertEqual(
                {
                    "computeAlternativeRoutes": False,
                    "departureTime": "2026-10-04T01:15:00Z",
                    "destination": {"placeId": "sentinel-destination-id"},
                    "origin": {"placeId": "sentinel-origin-id-b"},
                    "travelMode": "DRIVE",
                },
                json.loads(request.body),
            )
            safe = json.dumps(result, sort_keys=True)
            self.assertNotIn("diagnostic-route-response-sentinel", safe)
            self.assertNotIn("sentinel-origin-id-b", safe)
            self.assertNotIn("sentinel-destination-id", safe)

    def test_minimal_route_diagnostic_reports_safe_http_failure_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            review = gate.run_ishigaki_origin_candidate_review(
                trip_dir,
                transport=CannedTransport([_ambiguous_origin_response()], []),
                clock=lambda: NOW,
            )
            binding = review["review"]["selection_binding_v2"]
            transport = CannedTransport(
                [
                    _ambiguous_origin_response(),
                    _candidate_response(
                        place_id="sentinel-destination-id",
                        name="Yonehara Beach",
                        latitude=24.443,
                        longitude=124.191,
                    ),
                ],
                [
                    GoogleRoutesHttpResponse(
                        status_code=400,
                        body=b"diagnostic-route-error-sentinel",
                    )
                ],
            )

            result = gate.run_ishigaki_minimal_route_diagnostic(
                trip_dir,
                transport=transport,
                origin_choice="B",
                origin_selection_binding_v2=binding,
                clock=lambda: NOW + timedelta(minutes=1),
            )

            self.assertEqual("route_unavailable", result["status"])
            self.assertEqual(
                {
                    "requested": 1,
                    "request_accepted": False,
                    "diagnostic": "minimal_response_mask",
                    "persistence": "discarded_memory_only",
                    "failure_class": "invalid_request",
                },
                result["route"],
            )
            self.assertEqual(1, len(transport.diagnostic_route_requests))
            self.assertNotIn(
                "diagnostic-route-error-sentinel",
                json.dumps(result, sort_keys=True),
            )

    def test_undated_route_diagnostic_omits_departure_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            review = gate.run_ishigaki_origin_candidate_review(
                trip_dir,
                transport=CannedTransport([_ambiguous_origin_response()], []),
                clock=lambda: NOW,
            )
            binding = review["review"]["selection_binding_v2"]
            transport = CannedTransport(
                [
                    _ambiguous_origin_response(),
                    _candidate_response(
                        place_id="sentinel-destination-id",
                        name="Yonehara Beach",
                        latitude=24.443,
                        longitude=124.191,
                    ),
                ],
                [GoogleRoutesHttpResponse(status_code=200, body=b"opaque")],
            )

            result = gate.run_ishigaki_minimal_route_diagnostic(
                trip_dir,
                transport=transport,
                origin_choice="B",
                origin_selection_binding_v2=binding,
                include_departure=False,
                clock=lambda: NOW + timedelta(minutes=1),
            )

            self.assertEqual("minimal_route_diagnostic_completed", result["status"])
            self.assertEqual(
                "undated_minimal_response_mask",
                result["route"]["diagnostic"],
            )
            self.assertTrue(result["route"]["request_accepted"])
            self.assertEqual(1, len(transport.diagnostic_route_requests))
            body = json.loads(transport.diagnostic_route_requests[0][0].body)
            self.assertEqual(
                {
                    "computeAlternativeRoutes": False,
                    "destination": {"placeId": "sentinel-destination-id"},
                    "origin": {"placeId": "sentinel-origin-id-b"},
                    "travelMode": "DRIVE",
                },
                body,
            )
            self.assertNotIn("departureTime", body)

    def test_source_drift_during_identity_stops_before_merge_or_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            itinerary_path = trip_dir / "data" / "itinerary.json"

            def mutate_source() -> None:
                itinerary_path.write_text("{}", encoding="utf-8")

            transport = CannedTransport(
                [
                    _candidate_response(
                        place_id="sentinel-origin-id",
                        name="Kabira Bay Glass Boat",
                        latitude=24.457,
                        longitude=124.144,
                    )
                ],
                [],
                after_identity_call=mutate_source,
            )

            result = gate.run_ishigaki_provider_exit_gate(
                trip_dir,
                transport=transport,
                clock=lambda: NOW,
            )

            self.assertEqual("source_changed", result["status"])
            self.assertEqual(1, len(transport.identity_requests))
            self.assertEqual([], transport.route_requests)
            self.assertEqual(0, result["identities"]["verified_in_memory"])

    def test_origin_review_exposes_only_minimal_ephemeral_picker_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            before = self._tree_digest(trip_dir)
            transport = CannedTransport(
                [_ambiguous_origin_response()],
                [],
            )

            result = gate.run_ishigaki_origin_candidate_review(
                trip_dir,
                transport=transport,
                clock=lambda: NOW,
            )

            self.assertEqual("review_ready", result["status"])
            self.assertEqual(
                {"identity": 1, "route": 0, "total": 1},
                result["attempts"],
            )
            self.assertTrue(result["trip_files_modified"] is False)
            self.assertEqual(1, len(transport.identity_requests))
            self.assertEqual([], transport.route_requests)
            self.assertEqual(before, self._tree_digest(trip_dir))

            review = result["review"]
            self.assertEqual(
                [{"label": "Google Maps", "uri": None}],
                review["attributions"],
            )
            self.assertEqual("review_required", review["status"])
            self.assertTrue(review["selection_allowed"])
            self.assertFalse(review["results_truncated"])
            self.assertEqual(["A", "B"], [item["choice"] for item in review["candidates"]])
            self.assertTrue(all(item["eligible"] for item in review["candidates"]))
            self.assertTrue(all(item["exact_name_match"] for item in review["candidates"]))
            self.assertNotIn("candidate_ref", json.dumps(review, sort_keys=True))
            self.assertRegex(review["review_binding"], r"^[0-9a-f]{64}$")
            self.assertRegex(
                review["selection_binding_v2"],
                r"^[0-9a-f]{64}$",
            )
            safe = json.dumps(result, sort_keys=True)
            self.assertNotIn("sentinel-origin-id-a", safe)
            self.assertNotIn("sentinel-origin-id-b", safe)
            self.assertNotIn("24.457", safe)
            self.assertNotIn("124.144", safe)

    def test_user_confirmed_origin_choice_continues_with_one_destination_and_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            before = self._tree_digest(trip_dir)
            review_transport = CannedTransport(
                [_ambiguous_origin_response()],
                [],
            )
            review = gate.run_ishigaki_origin_candidate_review(
                trip_dir,
                transport=review_transport,
                clock=lambda: NOW,
            )
            binding = review["review"]["selection_binding_v2"]
            transport = CannedTransport(
                [
                    _ambiguous_origin_response(),
                    _candidate_response(
                        place_id="sentinel-destination-id",
                        name="Yonehara Beach",
                        latitude=24.443,
                        longitude=124.191,
                    ),
                ],
                [_route_response()],
            )

            result = gate.run_ishigaki_provider_exit_gate(
                trip_dir,
                transport=transport,
                origin_choice="B",
                origin_selection_binding_v2=binding,
                clock=lambda: NOW + timedelta(minutes=1),
            )

            self.assertEqual("completed_memory_only", result["status"])
            self.assertEqual(
                {"identity": 2, "route": 1, "total": 3},
                result["attempts"],
            )
            self.assertEqual(
                {"requested": 2, "verified_in_memory": 2},
                result["identities"],
            )
            self.assertEqual(2, len(transport.identity_requests))
            self.assertEqual(1, len(transport.route_requests))
            self.assertEqual(before, self._tree_digest(trip_dir))

    def test_changed_origin_review_stops_before_destination_or_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            initial = gate.run_ishigaki_origin_candidate_review(
                trip_dir,
                transport=CannedTransport([_ambiguous_origin_response()], []),
                clock=lambda: NOW,
            )
            binding = initial["review"]["selection_binding_v2"]
            changed_origin = _candidate_response(
                place_id="sentinel-origin-id-changed",
                name="Kabira Bay Glass Boat",
                latitude=24.457,
                longitude=124.144,
            )
            transport = CannedTransport([changed_origin], [])

            result = gate.run_ishigaki_provider_exit_gate(
                trip_dir,
                transport=transport,
                origin_choice="B",
                origin_selection_binding_v2=binding,
                clock=lambda: NOW + timedelta(minutes=1),
            )

            self.assertEqual("origin_review_changed", result["status"])
            self.assertEqual(1, len(transport.identity_requests))
            self.assertEqual([], transport.route_requests)
            self.assertEqual(0, result["identities"]["verified_in_memory"])

    def test_invalid_user_origin_choice_spends_no_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            transport = CannedTransport([], [])

            result = gate.run_ishigaki_provider_exit_gate(
                trip_dir,
                transport=transport,
                origin_choice="F",
                clock=lambda: NOW,
            )

            self.assertEqual("origin_choice_invalid", result["status"])
            self.assertEqual([], transport.identity_requests)
            self.assertEqual([], transport.route_requests)

    def test_missing_origin_selection_binding_spends_no_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            transport = CannedTransport([], [])

            result = gate.run_ishigaki_provider_exit_gate(
                trip_dir,
                transport=transport,
                origin_choice="B",
                clock=lambda: NOW,
            )

            self.assertEqual("origin_confirmation_required", result["status"])
            self.assertEqual([], transport.identity_requests)
            self.assertEqual([], transport.route_requests)

    def test_origin_review_source_drift_discards_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            itinerary_path = trip_dir / "data" / "itinerary.json"

            def mutate_source() -> None:
                itinerary_path.write_text("{}", encoding="utf-8")

            transport = CannedTransport(
                [
                    _candidate_response(
                        place_id="sentinel-origin-id",
                        name="Kabira Bay Glass Boat",
                        latitude=24.457,
                        longitude=124.144,
                    )
                ],
                [],
                after_identity_call=mutate_source,
            )

            result = gate.run_ishigaki_origin_candidate_review(
                trip_dir,
                transport=transport,
                clock=lambda: NOW,
            )

            self.assertEqual("source_changed", result["status"])
            self.assertEqual(1, len(transport.identity_requests))
            self.assertNotIn("review", result)

    def test_origin_review_marks_pagination_without_following_or_leaking_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            transport = CannedTransport(
                [
                    _candidate_response(
                        place_id="sentinel-origin-id",
                        name="Kabira Bay Glass Boat",
                        latitude=24.457,
                        longitude=124.144,
                        next_page_token="ephemeral-page-token-sentinel",
                    )
                ],
                [],
            )

            result = gate.run_ishigaki_origin_candidate_review(
                trip_dir,
                transport=transport,
                clock=lambda: NOW,
            )

            self.assertEqual("review_ready", result["status"])
            self.assertTrue(result["review"]["results_truncated"])
            self.assertEqual("review_required", result["review"]["status"])
            self.assertEqual(1, len(transport.identity_requests))
            self.assertEqual([], transport.route_requests)
            self.assertNotIn(
                "ephemeral-page-token-sentinel",
                json.dumps(result, sort_keys=True),
            )

    def test_origin_review_never_auto_promotes_a_unique_ready_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = self._write_trip(Path(temporary))
            before = self._tree_digest(trip_dir)
            transport = CannedTransport(
                [
                    _candidate_response(
                        place_id="sentinel-origin-id",
                        name="Kabira Bay Glass Boat",
                        latitude=24.457,
                        longitude=124.144,
                    )
                ],
                [],
            )

            result = gate.run_ishigaki_origin_candidate_review(
                trip_dir,
                transport=transport,
                clock=lambda: NOW,
            )

            self.assertEqual("review_ready", result["status"])
            self.assertEqual("ready", result["review"]["status"])
            self.assertFalse(result["review"]["selection_allowed"])
            self.assertEqual(1, len(transport.identity_requests))
            self.assertEqual([], transport.route_requests)
            self.assertEqual(before, self._tree_digest(trip_dir))


if __name__ == "__main__":
    unittest.main()
