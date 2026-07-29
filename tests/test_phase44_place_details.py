"""Offline contract and adversarial tests for Google Place Details v1."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

import trip_planner.facts as facts
import trip_planner.place_details as place_details
from trip_planner.evidence_session import EvidenceSession
from trip_planner.evidence_store import EvidenceStoreResult
from trip_planner.facts import (
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderProblemCode,
    ProviderProvenance,
    ProviderRequest,
    ProviderResultStatus,
    google_maps_policy_registry,
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
)
from trip_planner.place_details import (
    GOOGLE_PLACE_CURRENT_HOURS_FIELD_MASK,
    GOOGLE_PLACE_PROFILE_FIELD_MASK,
    GOOGLE_PLACE_REGULAR_HOURS_FIELD_MASK,
    GooglePlaceDetailsHttpResponse,
    GooglePlaceDetailsRequest,
    GooglePlaceDetailsTransportError,
    GooglePlaceDetailsTransportErrorKind,
    PlaceDetailsAttemptBudget,
    PlaceDetailsKind,
    build_google_place_details_http_request,
    build_google_place_details_request,
    execute_google_place_details,
    execute_google_place_details_batch,
)
from trip_planner.places_identity import (
    extract_fresh_google_place_endpoint,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
PLACE_ID = "ChIJ-place/private value"
LOCATION_ID = "venue"
REVISION = "a" * 64


class _DurableSource:
    def __init__(
        self,
        ledger: facts.EvidenceLedger,
        *,
        revision: str,
        purge_checked_at: datetime,
    ) -> None:
        self.ledger = ledger
        self.revision = revision
        self.purge_checked_at = purge_checked_at

    def load(self) -> EvidenceStoreResult:
        return EvidenceStoreResult(
            success=True,
            status="loaded",
            action="load",
            ledger=self.ledger,
            current_revision=self.revision,
            generation=1,
            purge_checked_at=self.purge_checked_at,
        )


class _Transport:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[object] = []

    def send(self, request: object, **_options: object) -> object:
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values)
        self.last = values[-1]

    def __call__(self) -> datetime:
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class _Fixture:
    def __init__(
        self,
        *,
        now: datetime = NOW,
        place_id: str = PLACE_ID,
        session_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.now = now
        self.place_id = place_id
        policies = google_maps_policy_registry(
            GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
        )
        identity_key = FactKey(
            kind=FactKind.PLACE_IDENTITY,
            subject_ids=(LOCATION_ID,),
            qualifiers=(("identity_provider", "google-places"),),
        )
        policy = policies.policy("google-place-id-v1")
        identity_request = ProviderRequest(
            provider_id="google-places",
            adapter_id="google-places",
            adapter_version="v1",
            operation="resolve-place",
            fact_keys=(identity_key,),
            policy_id=policy.policy_id,
            policy_digest=policy.policy_digest,
        )
        identity = FactObservation(
            key=identity_key,
            value=FactValue.from_payload(
                FactKind.PLACE_IDENTITY,
                {"provider_place_id": place_id},
            ),
            provenance=ProviderProvenance(
                provider_id="google-places",
                adapter_id="google-places",
                adapter_version="v1",
                request_fingerprint=identity_request.request_fingerprint,
                retention_policy_id=policy.policy_id,
                provider_record_id=place_id,
                attributions=(("Google Maps", None),),
            ),
            retrieved_at=now - timedelta(minutes=1),
            valid_until=now + timedelta(days=1),
            purge_at=None,
            confidence=1.0,
        )
        ledger = facts.EvidenceLedger(
            policies,
            (identity,),
            _token=facts._LEDGER_TOKEN,
        )
        self.source = _DurableSource(
            ledger,
            revision=REVISION,
            purge_checked_at=now,
        )
        self.session = EvidenceSession(
            self.source,
            clock=(
                (lambda: now)
                if session_clock is None
                else session_clock
            ),
        )
        self.load = self.session.load()
        self.snapshot = self.load.snapshot(evaluation_at=now)
        self.endpoint = extract_fresh_google_place_endpoint(
            self.snapshot,
            LOCATION_ID,
        )

    def request(
        self,
        kind: PlaceDetailsKind,
        *,
        target_start: date | None = None,
        target_end: date | None = None,
    ) -> GooglePlaceDetailsRequest:
        return build_google_place_details_request(
            self.snapshot,
            self.endpoint,
            kind,
            language_code="en-US",
            region_code="KR",
            target_start=target_start,
            target_end=target_end,
        )


def _response(
    body: object,
    *,
    status: int = 200,
    headers: tuple[tuple[str, str], ...] = (),
) -> GooglePlaceDetailsHttpResponse:
    encoded = (
        body
        if isinstance(body, bytes)
        else json.dumps(body, separators=(",", ":")).encode("utf-8")
    )
    return GooglePlaceDetailsHttpResponse(
        status,
        encoded,
        headers,
    )


def _point(
    value: date,
    hour: int = 0,
    minute: int = 0,
    *,
    truncated: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "date": {
            "year": value.year,
            "month": value.month,
            "day": value.day,
        },
        "day": (value.weekday() + 1) % 7,
        "hour": hour,
        "minute": minute,
    }
    if truncated:
        result["truncated"] = True
    return result


def _profile_body(place_id: str = PLACE_ID) -> dict[str, object]:
    return {
        "id": place_id,
        "displayName": {"text": "Fixture Place"},
        "businessStatus": "OPERATIONAL",
        "timeZone": {"id": "Asia/Seoul", "version": "fixture"},
        "location": {"latitude": 35.1, "longitude": 129.1},
        "attributions": [
            {
                "provider": "Fixture Provider",
                "providerUri": "https://example.test/attribution",
            }
        ],
    }


def _current_body(
    *,
    periods: list[object] | None = None,
    include_periods: bool = True,
    special_days: list[object] | None = None,
    place_id: str = PLACE_ID,
    timezone_name: str = "Asia/Seoul",
) -> dict[str, object]:
    hours: dict[str, object] = {
        "specialDays": [] if special_days is None else special_days,
    }
    if include_periods:
        hours["periods"] = (
            [
                {
                    "open": _point(date(2026, 7, 29), 9),
                    "close": _point(date(2026, 7, 29), 17),
                }
            ]
            if periods is None
            else periods
        )
    return {
        "id": place_id,
        "timeZone": {"id": timezone_name},
        "currentOpeningHours": hours,
        "attributions": [],
    }


def _regular_body(
    periods: list[object],
    *,
    place_id: str = PLACE_ID,
    timezone_name: str = "Asia/Seoul",
) -> dict[str, object]:
    return {
        "id": place_id,
        "timeZone": {"id": timezone_name},
        "regularOpeningHours": {"periods": periods},
        "attributions": [],
    }


def _run(
    request: GooglePlaceDetailsRequest,
    *outcomes: object,
    budget: int = 1,
    clock: object = None,
    **options: object,
) -> tuple[object, _Transport, PlaceDetailsAttemptBudget]:
    transport = _Transport(*outcomes)
    attempt_budget = PlaceDetailsAttemptBudget(budget)
    execution = execute_google_place_details(
        request,
        transport,
        attempt_budget=attempt_budget,
        clock=(lambda: NOW) if clock is None else clock,
        **options,
    )
    return execution, transport, attempt_budget


class PlaceDetailsRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _Fixture()

    def test_fixed_masks_exact_scope_encoded_get_and_redacted_views(
        self,
    ) -> None:
        expected_masks = {
            PlaceDetailsKind.PROFILE: GOOGLE_PLACE_PROFILE_FIELD_MASK,
            PlaceDetailsKind.CURRENT_HOURS:
                GOOGLE_PLACE_CURRENT_HOURS_FIELD_MASK,
            PlaceDetailsKind.REGULAR_HOURS:
                GOOGLE_PLACE_REGULAR_HOURS_FIELD_MASK,
        }
        for kind, mask in expected_masks.items():
            with self.subTest(kind=kind):
                dates = (
                    {}
                    if kind is PlaceDetailsKind.PROFILE
                    else {
                        "target_start": date(2026, 7, 29),
                        "target_end": date(2026, 7, 29),
                    }
                )
                request = self.fixture.request(kind, **dates)
                self.assertEqual(mask, request.field_mask)
                self.assertEqual(
                    tuple(sorted(dict(request.provider_request.query_scope))),
                    tuple(
                        name
                        for name, _value
                        in request.provider_request.query_scope
                    ),
                )
                http = build_google_place_details_http_request(request)
                parsed = urlsplit(http.url)
                self.assertEqual(
                    PLACE_ID,
                    unquote(parsed.path.rsplit("/", 1)[-1]),
                )
                self.assertEqual(
                    {
                        "languageCode": ["en-US"],
                        "regionCode": ["KR"],
                    },
                    parse_qs(parsed.query),
                )
                self.assertEqual(
                    {"X-Goog-FieldMask": mask},
                    dict(http.headers),
                )
                safe = json.dumps(
                    {
                        "request": request.to_binding_dict(),
                        "http": http.to_binding_dict(),
                        "request_repr": repr(request),
                        "http_repr": repr(http),
                    },
                    sort_keys=True,
                )
                self.assertNotIn(PLACE_ID, safe)

    def test_every_profile_scope_binding_rejects_tampering(self) -> None:
        request = self.fixture.request(PlaceDetailsKind.PROFILE)
        replacements: dict[str, object] = {
            "basis_evidence_revision": "b" * 64,
            "basis_snapshot_id": "b" * 64,
            "basis_store_revision": "b" * 64,
            "field_mask": "id",
            "identity_endpoint_id": "b" * 64,
            "identity_observation_id": "b" * 64,
            "identity_value_digest": "b" * 64,
            "language_code": "ja",
            "region_code": "JP",
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                scope = dict(request.provider_request.query_scope)
                scope[field] = replacement
                provider_request = ProviderRequest(
                    provider_id=request.provider_request.provider_id,
                    adapter_id=request.provider_request.adapter_id,
                    adapter_version=request.provider_request.adapter_version,
                    operation=request.provider_request.operation,
                    fact_keys=request.provider_request.fact_keys,
                    policy_id=request.provider_request.policy_id,
                    policy_digest=request.provider_request.policy_digest,
                    query_scope=tuple(scope.items()),
                )
                with self.assertRaises(FactContractError):
                    GooglePlaceDetailsRequest(
                        snapshot=request.snapshot,
                        endpoint=request.endpoint,
                        kind=request.kind,
                        language_code=request.language_code,
                        region_code=request.region_code,
                        target_start=request.target_start,
                        target_end=request.target_end,
                        provider_request=provider_request,
                        field_mask=request.field_mask,
                        _token=place_details._REQUEST_TOKEN,
                    )

    def test_target_and_kind_constraints_fail_before_transport(self) -> None:
        with self.assertRaises(FactContractError):
            self.fixture.request(
                PlaceDetailsKind.CURRENT_HOURS,
                target_start=date(2026, 7, 29),
                target_end=date(2026, 8, 5),
            )
        with self.assertRaises(FactContractError):
            self.fixture.request(
                PlaceDetailsKind.PROFILE,
                target_start=date(2026, 7, 29),
                target_end=date(2026, 7, 29),
            )

    def test_factory_rejects_endpoint_not_backed_by_active_snapshot(
        self,
    ) -> None:
        object.__setattr__(
            self.fixture.endpoint,
            "observation_id",
            "b" * 64,
        )
        with self.assertRaises(FactContractError):
            self.fixture.request(PlaceDetailsKind.PROFILE)


class PlaceDetailsDecodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _Fixture()

    def _request(
        self,
        kind: PlaceDetailsKind,
        target: date | None = None,
    ) -> GooglePlaceDetailsRequest:
        if kind is PlaceDetailsKind.PROFILE:
            return self.fixture.request(kind)
        assert target is not None
        return self.fixture.request(
            kind,
            target_start=target,
            target_end=target,
        )

    def test_profile_happy_path_normalizes_and_attributes(self) -> None:
        execution, _transport, _budget = _run(
            self._request(PlaceDetailsKind.PROFILE),
            _response(_profile_body()),
        )
        result = execution.result.result
        self.assertEqual(ProviderResultStatus.SUCCESS, result.status)
        observation = result.observations[0]
        self.assertEqual(
            {
                "business_status": "operational",
                "display_name": "Fixture Place",
                "latitude": 35.1,
                "longitude": 129.1,
                "provider_place_id": PLACE_ID,
                "timezone": "Asia/Seoul",
            },
            observation.value.payload,
        )
        self.assertEqual(
            (
                ("Fixture Provider", "https://example.test/attribution"),
                ("Google Maps", None),
            ),
            observation.provenance.attributions,
        )

    def test_current_periods_special_days_and_full_duration_payload(
        self,
    ) -> None:
        target = date(2026, 7, 29)
        body = _current_body(
            periods=[
                {
                    "open": _point(target, 22),
                    "close": _point(date(2026, 7, 30), 2),
                }
            ],
            special_days=[
                {
                    "date": {
                        "year": target.year,
                        "month": target.month,
                        "day": target.day,
                    }
                }
            ],
        )
        execution, _transport, _budget = _run(
            self._request(PlaceDetailsKind.CURRENT_HOURS, target),
            _response(body),
        )
        payload = execution.result.result.observations[0].value.payload
        self.assertEqual("current", payload["basis"])
        self.assertEqual("2026-07-29", payload["coverage_start"])
        self.assertEqual("2026-08-04", payload["coverage_end"])
        self.assertEqual(2, len(payload["intervals"]))
        self.assertNotIn(target.isoformat(), payload["closed_dates"])

    def test_current_always_open_and_explicitly_never_open(self) -> None:
        target = date(2026, 7, 29)
        always, _transport, _budget = _run(
            self._request(PlaceDetailsKind.CURRENT_HOURS, target),
            _response(
                _current_body(
                    periods=[{"open": _point(target)}],
                )
            ),
        )
        always_payload = (
            always.result.result.observations[0].value.payload
        )
        self.assertEqual(7, len(always_payload["intervals"]))
        self.assertEqual([], always_payload["closed_dates"])

        closed, _transport, _budget = _run(
            self._request(PlaceDetailsKind.CURRENT_HOURS, target),
            _response(_current_body(periods=[])),
        )
        closed_payload = (
            closed.result.result.observations[0].value.payload
        )
        self.assertEqual([], closed_payload["intervals"])
        self.assertEqual(7, len(closed_payload["closed_dates"]))

    def test_absent_periods_is_unknown_not_closed(self) -> None:
        target = date(2026, 7, 29)
        execution, _transport, _budget = _run(
            self._request(PlaceDetailsKind.CURRENT_HOURS, target),
            _response(_current_body(include_periods=False)),
        )
        result = execution.result.result
        self.assertEqual(ProviderResultStatus.FAILED, result.status)
        self.assertEqual(
            ProviderProblemCode.EMPTY_RESPONSE,
            result.problems[0].code,
        )

    def test_regular_24_7_overnight_and_unrepresented_day(self) -> None:
        sunday = date(2026, 8, 2)
        always, _transport, _budget = _run(
            self._request(PlaceDetailsKind.REGULAR_HOURS, sunday),
            _response(
                _regular_body(
                    [{"open": {"day": 0, "hour": 0, "minute": 0}}]
                )
            ),
        )
        always_payload = always.result.result.observations[0].value.payload
        self.assertEqual("regular_typical", always_payload["basis"])
        self.assertEqual(1, len(always_payload["intervals"]))
        self.assertEqual([], always_payload["closed_dates"])

        tuesday = date(2026, 7, 28)
        overnight, _transport, _budget = _run(
            self._request(PlaceDetailsKind.REGULAR_HOURS, tuesday),
            _response(
                _regular_body(
                    [
                        {
                            "open": {"day": 1, "hour": 22},
                            "close": {"day": 2, "hour": 2},
                        }
                    ]
                )
            ),
        )
        overnight_payload = (
            overnight.result.result.observations[0].value.payload
        )
        self.assertEqual(1, len(overnight_payload["intervals"]))
        self.assertEqual([], overnight_payload["closed_dates"])

        closed, _transport, _budget = _run(
            self._request(PlaceDetailsKind.REGULAR_HOURS, sunday),
            _response(
                _regular_body(
                    [
                        {
                            "open": {"day": 3, "hour": 9},
                            "close": {"day": 3, "hour": 17},
                        }
                    ]
                )
            ),
        )
        closed_payload = closed.result.result.observations[0].value.payload
        self.assertEqual([], closed_payload["intervals"])
        self.assertEqual([sunday.isoformat()], closed_payload["closed_dates"])

    def test_out_of_scope_id_horizon_and_special_day_are_typed(self) -> None:
        target = date(2026, 7, 29)
        cases = (
            (
                self._request(PlaceDetailsKind.PROFILE),
                _profile_body("different-place"),
                ProviderProblemCode.OUT_OF_SCOPE_RESULT,
            ),
            (
                self._request(
                    PlaceDetailsKind.CURRENT_HOURS,
                    date(2026, 8, 5),
                ),
                _current_body(),
                ProviderProblemCode.OUTSIDE_PROVIDER_HORIZON,
            ),
            (
                self._request(PlaceDetailsKind.CURRENT_HOURS, target),
                _current_body(
                    special_days=[
                        {
                            "date": {
                                "year": 2026,
                                "month": 8,
                                "day": 5,
                            }
                        }
                    ]
                ),
                ProviderProblemCode.INVALID_PROVIDER_RESPONSE,
            ),
        )
        for request, body, expected in cases:
            with self.subTest(expected=expected):
                execution, _transport, _budget = _run(
                    request,
                    _response(body),
                )
                self.assertEqual(
                    expected,
                    execution.result.result.problems[0].code,
                )

    def test_dst_ambiguous_and_nonexistent_points_fail_closed(self) -> None:
        cases = (
            (
                date(2026, 11, 1),
                {
                    "open": {"day": 0, "hour": 1, "minute": 30},
                    "close": {"day": 0, "hour": 2, "minute": 30},
                },
            ),
            (
                date(2026, 3, 8),
                {
                    "open": {"day": 0, "hour": 2, "minute": 30},
                    "close": {"day": 0, "hour": 3, "minute": 30},
                },
            ),
        )
        for target, period in cases:
            with self.subTest(target=target):
                request = self._request(
                    PlaceDetailsKind.REGULAR_HOURS,
                    target,
                )
                execution, _transport, _budget = _run(
                    request,
                    _response(
                        _regular_body(
                            [period],
                            timezone_name="America/New_York",
                        )
                    ),
                )
                self.assertEqual(
                    ProviderProblemCode.INVALID_PROVIDER_RESPONSE,
                    execution.result.result.problems[0].code,
                )

    def test_request_date_not_response_date_anchors_current_coverage(
        self,
    ) -> None:
        request_started = datetime(2026, 7, 29, 14, 58, tzinfo=UTC)
        fixture = _Fixture(now=request_started)
        request = fixture.request(
            PlaceDetailsKind.CURRENT_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 29),
        )
        sent_at = datetime(2026, 7, 29, 14, 59, tzinfo=UTC)
        completed_at = datetime(2026, 7, 29, 15, 1, tzinfo=UTC)
        clock = _SequenceClock(
            request_started,
            sent_at,
            completed_at,
            completed_at,
        )
        execution, _transport, _budget = _run(
            request,
            _response(_current_body()),
            clock=clock,
        )
        observation = execution.result.result.observations[0]
        self.assertEqual(sent_at, observation.retrieved_at)
        self.assertEqual(
            "2026-07-29",
            observation.value.payload["coverage_start"],
        )
        self.assertEqual(
            completed_at,
            execution.result.result.completed_at,
        )

    def test_strict_json_bounds_and_unknown_fields_fail_closed(self) -> None:
        request = self._request(PlaceDetailsKind.PROFILE)
        bodies = (
            b'{"id":"x","id":"x"}',
            b'{"value":NaN}',
            b"{" + b'"x":"' + (b"a" * (64 * 1024)) + b'"}',
            {
                **_profile_body(),
                "unexpected": "provider field outside mask",
            },
        )
        for body in bodies:
            with self.subTest(body_type=type(body).__name__):
                execution, _transport, _budget = _run(
                    request,
                    _response(body),
                )
                self.assertEqual(
                    ProviderProblemCode.INVALID_PROVIDER_RESPONSE,
                    execution.result.result.problems[0].code,
                )


class PlaceDetailsExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _Fixture()
        self.profile = self.fixture.request(PlaceDetailsKind.PROFILE)

    def test_http_status_mapping_is_typed_and_sanitized(self) -> None:
        cases = (
            (401, {}, ProviderProblemCode.AUTH_FAILED),
            (404, {}, ProviderProblemCode.NOT_FOUND),
            (400, {}, ProviderProblemCode.INVALID_PROVIDER_REQUEST),
            (429, {}, ProviderProblemCode.RATE_LIMITED),
            (503, {}, ProviderProblemCode.PROVIDER_UNAVAILABLE),
            (
                429,
                {"error": {"status": "RESOURCE_EXHAUSTED"}},
                ProviderProblemCode.QUOTA_EXHAUSTED,
            ),
        )
        for status, body, expected in cases:
            with self.subTest(status=status, expected=expected):
                execution, _transport, _budget = _run(
                    self.profile,
                    _response(body, status=status),
                )
                problem = execution.result.result.problems[0]
                self.assertEqual(expected, problem.code)
                self.assertNotIn(PLACE_ID, problem.message)

    def test_retry_after_budget_and_transport_failures(self) -> None:
        delays: list[float] = []
        execution, transport, budget = _run(
            self.profile,
            _response(
                {},
                status=429,
                headers=(("Retry-After", "7"),),
            ),
            _response(_profile_body()),
            budget=2,
            max_attempts=2,
            sleeper=delays.append,
        )
        self.assertEqual(ProviderResultStatus.SUCCESS, execution.result.result.status)
        self.assertEqual(2, execution.attempts_used)
        self.assertEqual(2, budget.used_attempts)
        self.assertEqual(2, len(transport.calls))
        self.assertEqual([7.0], delays)

        timeout = GooglePlaceDetailsTransportError(
            GooglePlaceDetailsTransportErrorKind.READ_TIMEOUT
        )
        failed, _transport, _budget = _run(
            self.profile,
            timeout,
        )
        self.assertEqual(
            ProviderProblemCode.TIMEOUT,
            failed.result.result.problems[0].code,
        )

        exhausted, transport, budget = _run(
            self.profile,
            budget=0,
        )
        self.assertEqual(0, exhausted.attempts_used)
        self.assertEqual(0, budget.used_attempts)
        self.assertEqual([], transport.calls)
        self.assertEqual(
            ProviderProblemCode.PROVIDER_BUDGET_EXHAUSTED,
            exhausted.result.result.problems[0].code,
        )

    def test_batch_merges_sequential_memory_results_without_disk_change(
        self,
    ) -> None:
        current = self.fixture.request(
            PlaceDetailsKind.CURRENT_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 29),
        )
        regular = self.fixture.request(
            PlaceDetailsKind.REGULAR_HOURS,
            target_start=date(2026, 8, 2),
            target_end=date(2026, 8, 2),
        )
        transport = _Transport(
            _response(_profile_body()),
            _response(_current_body()),
            _response(
                _regular_body(
                    [{"open": {"day": 0, "hour": 0, "minute": 0}}]
                )
            ),
        )
        batch = execute_google_place_details_batch(
            (self.profile, current, regular),
            transport,
            session=self.fixture.session,
            attempt_budget=PlaceDetailsAttemptBudget(3),
            clock=lambda: NOW,
        )
        self.assertEqual(3, batch.attempts_used)
        self.assertEqual(3, len(batch.executions))
        self.assertTrue(all(item.changed for item in batch.merges))
        self.assertEqual(REVISION, batch.current.store_revision)
        self.assertEqual(4, len(batch.current.ledger.observations))

    def test_batch_preflight_and_late_merge_reject_store_drift(self) -> None:
        self.fixture.source.revision = "b" * 64
        transport = _Transport(_response(_profile_body()))
        with self.assertRaises(FactContractError):
            execute_google_place_details_batch(
                (self.profile,),
                transport,
                session=self.fixture.session,
                attempt_budget=PlaceDetailsAttemptBudget(1),
                clock=lambda: NOW,
            )
        self.assertEqual([], transport.calls)

        fixture = _Fixture()
        request = fixture.request(PlaceDetailsKind.PROFILE)
        execution, _transport, _budget = _run(
            request,
            _response(_profile_body()),
        )
        fixture.source.revision = "c" * 64
        with self.assertRaises(FactContractError):
            fixture.session.merge(execution.result)


if __name__ == "__main__":
    unittest.main()
