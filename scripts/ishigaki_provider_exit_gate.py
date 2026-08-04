#!/usr/bin/env python3
"""Run one bounded, non-persistent Google provider exit gate for Ishigaki.

This is intentionally not a legacy cache refresh, migration, renderer, or
general live-provider CLI.  With explicit ``--live`` it performs at most two
Places Text Searches and one driving Routes request for a fixed public
activity leg.  The typed observations exist only in process memory and are
discarded before the command returns.  No file below ``trips/`` is written.

The gate refuses changed or unsafe input before every outbound stage and
before each in-memory merge.  It never prints credentials, raw queries,
provider IDs, addresses, coordinates, provider bodies, or route values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import socket
import ssl
import stat
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trip_planner.facts import (  # noqa: E402
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
    EvidenceLedger,
    EvidenceSnapshot,
    FactContractError,
    ProviderProblemCode,
    ProviderResultStatus,
    google_maps_policy_registry,
    merge_provider_result,
)
from trip_planner.places_identity import (  # noqa: E402
    GOOGLE_PLACE_IDENTITY_FIELD_MASK,
    PlaceIdentityCandidateAssessment,
    PlaceIdentityIntent,
    PlaceIdentityReview,
    PlaceIdentityReviewAuthority,
    PlaceIdentityReviewStatus,
    PlaceIdentityRequest,
    build_google_place_identity_request,
    evaluate_google_place_identity_candidates,
    extract_fresh_google_place_endpoint,
    finalize_google_place_identity_review,
)
from trip_planner.routes import (  # noqa: E402
    GOOGLE_ROUTES_COMPUTE_URL,
    GoogleRouteRequest,
    GoogleRoutesHttpRequest,
    GoogleRoutesHttpResponse,
    GoogleRoutesTransportError,
    GoogleRoutesTransportErrorKind,
    RouteAttemptBudget,
    RouteMode,
    build_google_route_request,
    build_google_routes_http_request,
    execute_google_route,
)


ISHIGAKI_PROVIDER_EXIT_GATE_VERSION = "ishigaki-provider-exit-gate/v2"
_ISHIGAKI_SLUG = "ishigaki-2026-10"
_PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_SOURCE_FILES = ("trip.json", "itinerary.json", "place_candidates.json")
_SOURCE_DIGEST_DOMAIN = b"trip-planner.ishigaki-provider-exit-gate/v1\0"
_REVIEW_BINDING_DOMAIN = b"trip-planner.ishigaki-origin-review/v1\0"
_SELECTION_BINDING_V2_DOMAIN = (
    b"trip-planner.ishigaki-origin-selection-binding/v2\0"
)
_MINIMAL_ROUTE_DIAGNOSTIC_FIELD_MASK = "routes.distanceMeters,routes.duration"
_MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_CONNECT_TIMEOUT_S = 3.0
_READ_TIMEOUT_S = 15.0
_TARGET_RADIUS_M = 5_000.0
_JAPAN_TIMEZONE = ZoneInfo("Asia/Tokyo")


class _GateError(ValueError):
    """A safe machine-readable refusal or source failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _TransportFailure(RuntimeError):
    """Transport condition with no provider-owned text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Target:
    key: str
    candidate_name: str


_TARGETS = (
    _Target("origin", "Kabira Bay Glass Boat"),
    _Target("destination", "Yonehara Beach"),
)


@dataclass(frozen=True, slots=True, repr=False)
class GooglePlacesHttpRequest:
    """Runtime-only Text Search request with a redacted representation."""

    url: str
    body: bytes = field(repr=False)
    field_mask: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if self.url != _PLACES_TEXT_SEARCH_URL:
            raise ValueError("unexpected Places Text Search endpoint")
        if not isinstance(self.body, bytes) or not self.body:
            raise ValueError("Places request body must be non-empty bytes")
        if self.field_mask != GOOGLE_PLACE_IDENTITY_FIELD_MASK:
            raise ValueError("Places request field mask differs from identity contract")

    @property
    def headers(self) -> Mapping[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-FieldMask": ",".join(self.field_mask),
        }

    def __repr__(self) -> str:
        return (
            "GooglePlacesHttpRequest("
            f"url={self.url!r}, body_bytes={len(self.body)}, "
            f"body_digest={hashlib.sha256(self.body).hexdigest()!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _MinimalRouteDiagnosticHttpRequest:
    """One diagnostic-only Routes request with a deliberately narrow mask."""

    url: str
    body: bytes = field(repr=False)
    field_mask: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.url != GOOGLE_ROUTES_COMPUTE_URL:
            raise ValueError("unexpected minimal Routes diagnostic endpoint")
        if not isinstance(self.body, bytes) or not self.body:
            raise ValueError("minimal Routes diagnostic body must be non-empty")
        if self.field_mask != _MINIMAL_ROUTE_DIAGNOSTIC_FIELD_MASK:
            raise ValueError("unexpected minimal Routes diagnostic field mask")

    @property
    def headers(self) -> Mapping[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-FieldMask": self.field_mask,
        }

    def __repr__(self) -> str:
        return (
            "_MinimalRouteDiagnosticHttpRequest("
            f"url={self.url!r}, body_bytes={len(self.body)}, "
            f"body_digest={hashlib.sha256(self.body).hexdigest()!r})"
        )


@dataclass(frozen=True, slots=True)
class _PilotScope:
    data_dir: Path
    source_revision: str
    intents: tuple[PlaceIdentityIntent, PlaceIdentityIntent]
    departure_at: str


class _ProviderTransport(Protocol):
    def search_place_identity(
        self,
        request: GooglePlacesHttpRequest,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        """Perform one runtime-only Google Places request."""

    def send(
        self,
        request: GoogleRoutesHttpRequest,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        """Perform one runtime-only Google Routes request."""

    def send_route_diagnostic(
        self,
        request: _MinimalRouteDiagnosticHttpRequest,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        """Perform one non-evidence diagnostic Routes request."""


class GoogleMapsHttpTransport:
    """Small stdlib transport; credentials and bodies never reach safe output."""

    def __init__(self, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Google Maps API key is required")
        self._api_key = api_key

    def search_place_identity(
        self,
        request: GooglePlacesHttpRequest,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        return self._post(
            request.url,
            request.body,
            request.headers,
            connect_timeout_s=connect_timeout_s,
            read_timeout_s=read_timeout_s,
        )

    def send(
        self,
        request: GoogleRoutesHttpRequest,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        return self._post(
            request.url,
            request.body,
            request.headers,
            connect_timeout_s=connect_timeout_s,
            read_timeout_s=read_timeout_s,
        )

    def send_route_diagnostic(
        self,
        request: _MinimalRouteDiagnosticHttpRequest,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        return self._post(
            request.url,
            request.body,
            request.headers,
            connect_timeout_s=connect_timeout_s,
            read_timeout_s=read_timeout_s,
        )

    def _post(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        request_headers = dict(headers)
        request_headers["X-Goog-Api-Key"] = self._api_key
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method="POST",
        )
        # urllib applies one socket timeout to connect and reads.  The stricter
        # connect value is still passed by typed Routes; this small live pilot
        # keeps the total response wait bounded by the read budget.
        timeout_s = max(connect_timeout_s, read_timeout_s)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return GoogleRoutesHttpResponse(
                    status_code=int(response.status),
                    body=_read_bounded(response),
                    headers=_safe_headers(response),
                )
        except urllib.error.HTTPError as error:
            try:
                body_bytes = _read_bounded(error)
            except _TransportFailure:
                body_bytes = b""
            return GoogleRoutesHttpResponse(
                status_code=int(error.code),
                body=body_bytes,
                headers=_safe_headers(error),
            )
        except (socket.timeout, TimeoutError):
            raise _TransportFailure("timeout") from None
        except ssl.SSLError:
            raise _TransportFailure("tls") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise _TransportFailure("timeout") from None
            if isinstance(error.reason, ssl.SSLError):
                raise _TransportFailure("tls") from None
            raise _TransportFailure("network") from None
        except OSError:
            raise _TransportFailure("network") from None


def build_google_places_text_search_http_request(
    request: PlaceIdentityRequest,
) -> GooglePlacesHttpRequest:
    """Materialize one exact identity request at the runtime-only boundary."""

    if type(request) is not PlaceIdentityRequest:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "identity HTTP request requires an exact identity request.",
        )
    intent = request.intent
    body: dict[str, object] = {
        "textQuery": intent.text_query,
        "languageCode": intent.language_code,
        "regionCode": intent.region_code,
        "pageSize": 5,
    }
    if intent.radius_m is not None:
        body["locationBias"] = {
            "circle": {
                "center": {
                    "latitude": intent.latitude,
                    "longitude": intent.longitude,
                },
                "radius": intent.radius_m,
            }
        }
    return GooglePlacesHttpRequest(
        url=_PLACES_TEXT_SEARCH_URL,
        body=json.dumps(
            body,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        field_mask=request.field_mask,
    )


def _build_minimal_route_diagnostic_http_request(
    request: GoogleRouteRequest,
    *,
    include_departure: bool,
) -> _MinimalRouteDiagnosticHttpRequest:
    """Build a bounded diagnostic request that cannot become route evidence."""

    if type(request) is not GoogleRouteRequest:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "minimal route diagnostic requires an exact GoogleRouteRequest.",
        )
    if request.mode is not RouteMode.DRIVING:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "minimal route diagnostic is fixed to driving.",
        )
    if type(include_departure) is not bool:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "minimal route diagnostic include_departure must be bool.",
        )
    normal_request = build_google_routes_http_request(request)
    if include_departure:
        body = normal_request.body
    else:
        body = json.dumps(
            {
                "computeAlternativeRoutes": False,
                "destination": {
                    "placeId": request.destination.provider_place_id,
                },
                "origin": {"placeId": request.origin.provider_place_id},
                "travelMode": request.mode.google_value,
            },
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    return _MinimalRouteDiagnosticHttpRequest(
        url=normal_request.url,
        body=body,
        field_mask=_MINIMAL_ROUTE_DIAGNOSTIC_FIELD_MASK,
    )


def run_ishigaki_provider_exit_gate(
    trip_path: str | Path,
    *,
    transport: _ProviderTransport,
    origin_choice: str | None = None,
    origin_selection_binding_v2: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Run the fixed 2-identity + 1-route gate and discard all evidence.

    ``origin_choice`` is accepted only alongside the stable v2 binding emitted
    by a prior read-only review.  It selects an eligible A-E candidate from a
    newly fetched matching candidate set; neither value is persisted.
    """

    try:
        scope = _load_scope(trip_path)
    except _GateError as error:
        return _report(error.code)
    choice = _choice(origin_choice)
    if origin_choice is not None and choice is None:
        return _report("origin_choice_invalid")
    if (choice is None) != (origin_selection_binding_v2 is None):
        return _report("origin_confirmation_required")
    if origin_selection_binding_v2 is not None and not _digest_text(
        origin_selection_binding_v2
    ):
        return _report("origin_confirmation_required")

    policies = google_maps_policy_registry(GOOGLE_MAPS_NON_EEA_POLICY_PROFILE)
    ledger = EvidenceLedger(policies)
    identity_attempts = 0
    identity_verified = 0

    for index, intent in enumerate(scope.intents):
        if index == 0 and choice is not None:
            ledger, status, attempts = _resolve_user_confirmed_identity(
                scope,
                ledger,
                intent,
                choice,
                origin_selection_binding_v2,
                transport,
                clock,
            )
        else:
            ledger, status, attempts = _resolve_identity(
                scope,
                ledger,
                intent,
                transport,
                clock,
            )
        identity_attempts += attempts
        if ledger is None:
            return _report(
                status,
                identity_attempts=identity_attempts,
                identity_verified=identity_verified,
            )
        identity_verified += 1

    if not _source_matches(scope.data_dir, scope.source_revision):
        return _report(
            "source_changed",
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
        )

    snapshot = _snapshot(ledger, clock)
    try:
        origin = extract_fresh_google_place_endpoint(
            snapshot,
            scope.intents[0].location_id,
        )
        destination = extract_fresh_google_place_endpoint(
            snapshot,
            scope.intents[1].location_id,
        )
        route_request = build_google_route_request(
            snapshot,
            origin,
            destination,
            RouteMode.DRIVING,
            departure_at=scope.departure_at,
        )
        execution = execute_google_route(
            route_request,
            transport,
            attempt_budget=RouteAttemptBudget(1),
            clock=clock,
            max_attempts=1,
            connect_timeout_s=_CONNECT_TIMEOUT_S,
            read_timeout_s=_READ_TIMEOUT_S,
        )
    except FactContractError:
        return _report(
            "route_contract_rejected",
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
        )
    except _TransportFailure as error:
        return _report(
            _route_transport_status(error.code),
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
            route_attempts=1,
        )

    route_attempts = execution.attempts_used
    route_result = execution.primary_result.result
    if route_result.status is not ProviderResultStatus.SUCCESS:
        return _report(
            "route_unavailable",
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
            route_attempts=route_attempts,
            route_failure_class=_route_failure_class(route_result.problems),
        )
    if not _source_matches(scope.data_dir, scope.source_revision):
        return _report(
            "source_changed",
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
            route_attempts=route_attempts,
        )
    try:
        # This is intentionally only a typed in-memory contract check.  The
        # merged route is MEMORY_ONLY and discarded immediately afterwards.
        merge_provider_result(
            ledger,
            execution.primary_result,
            purge_now=_now(clock),
        )
    except FactContractError:
        return _report(
            "route_contract_rejected",
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
            route_attempts=route_attempts,
        )
    if not _source_matches(scope.data_dir, scope.source_revision):
        return _report(
            "source_changed",
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
            route_attempts=route_attempts,
        )
    return _report(
        "completed_memory_only",
        identity_attempts=identity_attempts,
        identity_verified=identity_verified,
        route_attempts=route_attempts,
        route_checked=True,
    )


def run_ishigaki_minimal_route_diagnostic(
    trip_path: str | Path,
    *,
    transport: _ProviderTransport,
    origin_choice: str | None,
    origin_selection_binding_v2: str | None,
    include_departure: bool = True,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Test only whether the provider accepts a minimum Routes response mask.

    This is deliberately separate from the normal typed Routes adapter.  It
    never decodes route values into a fact, merges evidence, or reports any
    route value.  It exists solely to isolate a rejected full response mask.
    """

    if type(include_departure) is not bool:
        return _minimal_route_diagnostic_report("invalid_diagnostic_mode")
    diagnostic = (
        "minimal_response_mask"
        if include_departure
        else "undated_minimal_response_mask"
    )
    try:
        scope = _load_scope(trip_path)
    except _GateError as error:
        return _minimal_route_diagnostic_report(error.code, diagnostic=diagnostic)
    choice = _choice(origin_choice)
    if origin_choice is not None and choice is None:
        return _minimal_route_diagnostic_report(
            "origin_choice_invalid",
            diagnostic=diagnostic,
        )
    if (
        choice is None
        or origin_selection_binding_v2 is None
        or not _digest_text(origin_selection_binding_v2)
    ):
        return _minimal_route_diagnostic_report(
            "origin_confirmation_required",
            diagnostic=diagnostic,
        )

    ledger = EvidenceLedger(
        google_maps_policy_registry(GOOGLE_MAPS_NON_EEA_POLICY_PROFILE)
    )
    identity_attempts = 0
    identity_verified = 0
    for index, intent in enumerate(scope.intents):
        if index == 0:
            ledger, status, attempts = _resolve_user_confirmed_identity(
                scope,
                ledger,
                intent,
                choice,
                origin_selection_binding_v2,
                transport,
                clock,
            )
        else:
            ledger, status, attempts = _resolve_identity(
                scope,
                ledger,
                intent,
                transport,
                clock,
            )
        identity_attempts += attempts
        if ledger is None:
            return _minimal_route_diagnostic_report(
                status,
                diagnostic=diagnostic,
                identity_attempts=identity_attempts,
                identity_verified=identity_verified,
            )
        identity_verified += 1

    if not _source_matches(scope.data_dir, scope.source_revision):
        return _minimal_route_diagnostic_report(
            "source_changed",
            diagnostic=diagnostic,
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
        )
    snapshot = _snapshot(ledger, clock)
    try:
        origin = extract_fresh_google_place_endpoint(
            snapshot,
            scope.intents[0].location_id,
        )
        destination = extract_fresh_google_place_endpoint(
            snapshot,
            scope.intents[1].location_id,
        )
        route_request = build_google_route_request(
            snapshot,
            origin,
            destination,
            RouteMode.DRIVING,
            departure_at=scope.departure_at,
        )
        diagnostic_request = _build_minimal_route_diagnostic_http_request(
            route_request,
            include_departure=include_departure,
        )
        if not _source_matches(scope.data_dir, scope.source_revision):
            return _minimal_route_diagnostic_report(
                "source_changed",
                diagnostic=diagnostic,
                identity_attempts=identity_attempts,
                identity_verified=identity_verified,
            )
        response = transport.send_route_diagnostic(
            diagnostic_request,
            connect_timeout_s=_CONNECT_TIMEOUT_S,
            read_timeout_s=_READ_TIMEOUT_S,
        )
    except FactContractError:
        return _minimal_route_diagnostic_report(
            "route_contract_rejected",
            diagnostic=diagnostic,
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
        )
    except _TransportFailure as error:
        return _minimal_route_diagnostic_report(
            _route_transport_status(error.code),
            diagnostic=diagnostic,
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
            route_attempts=1,
        )

    if not _source_matches(scope.data_dir, scope.source_revision):
        return _minimal_route_diagnostic_report(
            "source_changed",
            diagnostic=diagnostic,
            identity_attempts=identity_attempts,
            identity_verified=identity_verified,
            route_attempts=1,
        )
    status, accepted, failure_class = _minimal_route_diagnostic_outcome(response)
    return _minimal_route_diagnostic_report(
        status,
        diagnostic=diagnostic,
        identity_attempts=identity_attempts,
        identity_verified=identity_verified,
        route_attempts=1,
        route_accepted=accepted,
        route_failure_class=failure_class,
    )


def run_ishigaki_origin_candidate_review(
    trip_path: str | Path,
    *,
    transport: _ProviderTransport,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Fetch one origin candidate set for human review without promotion."""

    try:
        scope = _load_scope(trip_path)
    except _GateError as error:
        return _candidate_review_report(error.code)

    ledger = EvidenceLedger(
        google_maps_policy_registry(GOOGLE_MAPS_NON_EEA_POLICY_PROFILE)
    )
    _snapshot_value, review, status, attempts = _fetch_identity_review(
        scope,
        ledger,
        scope.intents[0],
        transport,
        clock,
    )
    if review is None:
        return _candidate_review_report(status, attempts=attempts)
    return _candidate_review_report(
        "review_ready",
        attempts=attempts,
        review=review,
        source_revision=scope.source_revision,
    )


def _resolve_identity(
    scope: _PilotScope,
    ledger: EvidenceLedger,
    intent: PlaceIdentityIntent,
    transport: _ProviderTransport,
    clock: Callable[[], datetime],
) -> tuple[EvidenceLedger | None, str, int]:
    snapshot, review, status, attempts = _fetch_identity_review(
        scope,
        ledger,
        intent,
        transport,
        clock,
    )
    if snapshot is None or review is None:
        return None, status, attempts
    if review.status is PlaceIdentityReviewStatus.REVIEW_REQUIRED:
        return None, "identity_review_required", attempts
    if review.status is not PlaceIdentityReviewStatus.READY:
        return None, "identity_not_verified", attempts
    try:
        if not _source_matches(scope.data_dir, scope.source_revision):
            return None, "source_changed", attempts
        authorized = finalize_google_place_identity_review(
            review,
            snapshot,
            PlaceIdentityReviewAuthority(
                reviewer_id="provider-exit-gate",
                clock=clock,
            ),
        )
        if not _source_matches(scope.data_dir, scope.source_revision):
            return None, "source_changed", attempts
        merged = merge_provider_result(
            ledger,
            authorized,
            purge_now=_now(clock),
        )
    except FactContractError:
        return None, "invalid_provider_response", attempts
    if not _source_matches(scope.data_dir, scope.source_revision):
        return None, "source_changed", attempts
    return merged.ledger, "completed", attempts


def _resolve_user_confirmed_identity(
    scope: _PilotScope,
    ledger: EvidenceLedger,
    intent: PlaceIdentityIntent,
    choice: str,
    expected_selection_binding_v2: str | None,
    transport: _ProviderTransport,
    clock: Callable[[], datetime],
) -> tuple[EvidenceLedger | None, str, int]:
    """Promote one fresh, eligible candidate selected by the user this turn."""

    snapshot, review, status, attempts = _fetch_identity_review(
        scope,
        ledger,
        intent,
        transport,
        clock,
    )
    if snapshot is None or review is None:
        return None, status, attempts
    if (
        expected_selection_binding_v2 is None
        or review.status is not PlaceIdentityReviewStatus.REVIEW_REQUIRED
        or review.results_truncated
        or _selection_binding_v2(scope.source_revision, review)
        != expected_selection_binding_v2
    ):
        return None, "origin_review_changed", attempts
    assessment = _choice_assessment(review, choice)
    if assessment is None or not assessment.eligible:
        return None, "origin_choice_invalid", attempts
    try:
        if not _source_matches(scope.data_dir, scope.source_revision):
            return None, "source_changed", attempts
        authority = PlaceIdentityReviewAuthority(
            reviewer_id="user-confirmed-origin-choice",
            clock=clock,
        )
        grant = authority.issue_grant(
            review,
            assessment.candidate.candidate_id,
        )
        authorized = finalize_google_place_identity_review(
            review,
            snapshot,
            authority,
            grant,
        )
        if not _source_matches(scope.data_dir, scope.source_revision):
            return None, "source_changed", attempts
        merged = merge_provider_result(
            ledger,
            authorized,
            purge_now=_now(clock),
        )
    except FactContractError:
        return None, "origin_choice_rejected", attempts
    if not _source_matches(scope.data_dir, scope.source_revision):
        return None, "source_changed", attempts
    return merged.ledger, "completed", attempts


def _fetch_identity_review(
    scope: _PilotScope,
    ledger: EvidenceLedger,
    intent: PlaceIdentityIntent,
    transport: _ProviderTransport,
    clock: Callable[[], datetime],
) -> tuple[
    EvidenceSnapshot | None,
    PlaceIdentityReview | None,
    str,
    int,
]:
    """Fetch and evaluate exactly one identity candidate set, never promote it."""

    if not _source_matches(scope.data_dir, scope.source_revision):
        return None, None, "source_changed", 0
    snapshot = _snapshot(ledger, clock)
    try:
        request = build_google_place_identity_request(intent, snapshot)
        response = transport.search_place_identity(
            build_google_places_text_search_http_request(request),
            connect_timeout_s=_CONNECT_TIMEOUT_S,
            read_timeout_s=_READ_TIMEOUT_S,
        )
    except _TransportFailure as error:
        return None, None, _identity_transport_status(error.code), 1
    except FactContractError:
        return None, None, "identity_contract_rejected", 0
    if type(response) is not GoogleRoutesHttpResponse:
        return None, None, "invalid_provider_response", 1
    if not 200 <= response.status_code < 300:
        return None, None, _identity_http_status(response.status_code), 1
    try:
        raw_response = json.loads(response.body.decode("utf-8"))
        if not isinstance(raw_response, Mapping):
            return None, None, "invalid_provider_response", 1
        if not _source_matches(scope.data_dir, scope.source_revision):
            return None, None, "source_changed", 1
        review = evaluate_google_place_identity_candidates(
            request,
            raw_response,
            completed_at=_now(clock),
            attempts_used=1,
        )
        if not _source_matches(scope.data_dir, scope.source_revision):
            return None, None, "source_changed", 1
    except (
        FactContractError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        MemoryError,
        RecursionError,
    ):
        return None, None, "invalid_provider_response", 1
    return snapshot, review, "completed", 1


def _load_scope(trip_path: str | Path) -> _PilotScope:
    data_dir = _data_dir(trip_path)
    if (data_dir / "plan.json").exists() or (data_dir / "plan.json").is_symlink():
        raise _GateError("scope_not_legacy")
    source_revision, documents = _read_source_documents(data_dir)
    trip = documents["trip.json"]
    itinerary = documents["itinerary.json"]
    candidates = documents["place_candidates.json"]
    if trip.get("slug") != _ISHIGAKI_SLUG:
        raise _GateError("scope_not_ishigaki")
    candidate_queries = _target_queries(candidates)
    activities = _target_activities(itinerary, candidate_queries)
    origin, destination = activities
    if origin["date"] != destination["date"]:
        raise _GateError("invalid_pilot_scope")
    departure_at = _departure_at(origin["date"], origin["time"])
    intents = tuple(
        PlaceIdentityIntent(
            location_id=f"pilot-{target.key}",
            text_query=candidate_queries[target.candidate_name],
            expected_name=target.candidate_name,
            region_code="JP",
            language_code="en",
            expected_locality="Ishigaki",
            latitude=activity["latitude"],
            longitude=activity["longitude"],
            radius_m=_TARGET_RADIUS_M,
        )
        for target, activity in zip(_TARGETS, activities, strict=True)
    )
    return _PilotScope(
        data_dir=data_dir,
        source_revision=source_revision,
        intents=(intents[0], intents[1]),
        departure_at=departure_at,
    )


def _data_dir(trip_path: str | Path) -> Path:
    path = Path(trip_path)
    data_dir = path / "data" if (path / "data").is_dir() else path
    if data_dir.is_symlink() or not data_dir.is_dir():
        raise _GateError("invalid_pilot_scope")
    return data_dir


def _read_source_documents(data_dir: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    digest = hashlib.sha256(_SOURCE_DIGEST_DOMAIN)
    documents: dict[str, dict[str, Any]] = {}
    for filename in _SOURCE_FILES:
        path = data_dir / filename
        if path.is_symlink():
            raise _GateError("unsafe_input")
        try:
            metadata = path.stat()
        except OSError as error:
            raise _GateError("invalid_pilot_scope") from error
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SOURCE_FILE_BYTES:
            raise _GateError("unsafe_input")
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, MemoryError, RecursionError) as error:
            raise _GateError("invalid_pilot_scope") from error
        if not isinstance(value, dict):
            raise _GateError("invalid_pilot_scope")
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        documents[filename] = value
    return digest.hexdigest(), documents


def _source_matches(data_dir: Path, expected_revision: str) -> bool:
    try:
        revision, _documents = _read_source_documents(data_dir)
    except _GateError:
        return False
    return revision == expected_revision


def _target_queries(candidates: Mapping[str, Any]) -> dict[str, str]:
    values = candidates.get("candidates")
    if not isinstance(values, list):
        raise _GateError("invalid_pilot_scope")
    queries: dict[str, str] = {}
    wanted = {target.candidate_name for target in _TARGETS}
    for candidate in values:
        if not isinstance(candidate, Mapping):
            continue
        name = candidate.get("name")
        if not isinstance(name, str) or name not in wanted:
            continue
        query = _text(candidate.get("maps_query"))
        if query is None or name in queries:
            raise _GateError("invalid_pilot_scope")
        queries[name] = query
    if set(queries) != wanted:
        raise _GateError("invalid_pilot_scope")
    return queries


def _target_activities(
    itinerary: Mapping[str, Any],
    queries: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    days = itinerary.get("days")
    if not isinstance(days, list):
        raise _GateError("invalid_pilot_scope")
    matches: dict[str, list[dict[str, Any]]] = {query: [] for query in queries.values()}
    for day in days:
        if not isinstance(day, Mapping):
            continue
        date = _text(day.get("date"))
        places = day.get("places")
        if date is None or not isinstance(places, list):
            continue
        for place in places:
            if not isinstance(place, Mapping):
                continue
            query = place.get("maps_query")
            if not isinstance(query, str) or query not in matches:
                continue
            time = _text(place.get("time"))
            latitude = _coordinate(place.get("lat"), -90.0, 90.0)
            longitude = _coordinate(place.get("lng"), -180.0, 180.0)
            if time is None or latitude is None or longitude is None:
                raise _GateError("invalid_pilot_scope")
            matches[query].append(
                {
                    "date": date,
                    "time": time,
                    "latitude": latitude,
                    "longitude": longitude,
                }
            )
    selected: list[dict[str, Any]] = []
    for target in _TARGETS:
        query = queries[target.candidate_name]
        choices = matches[query]
        if len(choices) != 1:
            raise _GateError("invalid_pilot_scope")
        selected.append(choices[0])
    return selected[0], selected[1]


def _departure_at(date: str, time: str) -> str:
    try:
        local = datetime.fromisoformat(f"{date}T{time}")
    except ValueError as error:
        raise _GateError("invalid_pilot_scope") from error
    if local.tzinfo is not None:
        raise _GateError("invalid_pilot_scope")
    return local.replace(tzinfo=_JAPAN_TIMEZONE).isoformat()


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and len(normalized) <= 512 else None


def _coordinate(value: object, lower: float, upper: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not lower <= number <= upper:
        return None
    return 0.0 if number == 0 else number


def _snapshot(
    ledger: EvidenceLedger,
    clock: Callable[[], datetime],
) -> EvidenceSnapshot:
    now = _now(clock)
    return EvidenceSnapshot.from_ledger(
        ledger,
        evaluation_at=now,
        purge_now=now,
    )


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FactContractError("INVALID_PROVIDER_RESPONSE", "clock must return aware datetime")
    return value.astimezone(timezone.utc)


def _read_bounded(response: object) -> bytes:
    reader = getattr(response, "read", None)
    if not callable(reader):
        raise _TransportFailure("network")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = reader(min(8192, _MAX_RESPONSE_BYTES + 1 - total))
        if not isinstance(chunk, bytes):
            raise _TransportFailure("network")
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise _TransportFailure("response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_headers(response: object) -> tuple[tuple[str, str], ...]:
    headers = getattr(response, "headers", None)
    items = getattr(headers, "items", None)
    if not callable(items):
        return ()
    try:
        raw = list(items())[:32]
    except (OSError, TypeError, ValueError):
        return ()
    return tuple(
        (str(name), str(value))
        for name, value in raw
        if isinstance(name, str) and isinstance(value, str)
    )


def _identity_http_status(status_code: int) -> str:
    if status_code in (401, 403):
        return "provider_auth_failed"
    if status_code == 429:
        return "provider_rate_limited"
    if 500 <= status_code <= 599:
        return "provider_unavailable"
    return "provider_rejected"


def _identity_transport_status(code: str) -> str:
    return {
        "timeout": "provider_timeout",
        "tls": "provider_unavailable",
        "network": "provider_unavailable",
        "response_too_large": "invalid_provider_response",
    }.get(code, "provider_unavailable")


def _route_transport_status(code: str) -> str:
    return _identity_transport_status(code)


def _report(
    status: str,
    *,
    identity_attempts: int = 0,
    identity_verified: int = 0,
    route_attempts: int = 0,
    route_checked: bool = False,
    route_failure_class: str | None = None,
) -> dict[str, object]:
    route: dict[str, object] = {
        "requested": 1,
        "checked": route_checked,
        "persistence": "discarded_memory_only",
    }
    if route_failure_class is not None:
        route["failure_class"] = route_failure_class
    return {
        "contract_version": ISHIGAKI_PROVIDER_EXIT_GATE_VERSION,
        "status": status,
        "attempts": {
            "identity": identity_attempts,
            "route": route_attempts,
            "total": identity_attempts + route_attempts,
        },
        "identities": {
            "requested": len(_TARGETS),
            "verified_in_memory": identity_verified,
        },
        "route": route,
        "trip_files_modified": False,
    }


def _minimal_route_diagnostic_outcome(
    response: object,
) -> tuple[str, bool, str | None]:
    """Classify only transport acceptance; route data remains unparsed."""

    if type(response) is not GoogleRoutesHttpResponse:
        return "route_diagnostic_invalid_response", False, "invalid_response"
    if not 200 <= response.status_code < 300:
        return "route_unavailable", False, _minimal_route_failure_class(
            response.status_code
        )
    return "minimal_route_diagnostic_completed", True, None


def _minimal_route_failure_class(status_code: int) -> str:
    return {
        400: "invalid_request",
        401: "auth_failed",
        403: "auth_failed",
        404: "not_found",
        408: "timeout",
        429: "rate_limited",
    }.get(
        status_code,
        "provider_unavailable",
    )


def _minimal_route_diagnostic_report(
    status: str,
    *,
    diagnostic: str = "minimal_response_mask",
    identity_attempts: int = 0,
    identity_verified: int = 0,
    route_attempts: int = 0,
    route_accepted: bool = False,
    route_failure_class: str | None = None,
) -> dict[str, object]:
    route: dict[str, object] = {
        "requested": 1,
        "request_accepted": route_accepted,
        "diagnostic": diagnostic,
        "persistence": "discarded_memory_only",
    }
    if route_failure_class is not None:
        route["failure_class"] = route_failure_class
    return {
        "contract_version": ISHIGAKI_PROVIDER_EXIT_GATE_VERSION,
        "status": status,
        "attempts": {
            "identity": identity_attempts,
            "route": route_attempts,
            "total": identity_attempts + route_attempts,
        },
        "identities": {
            "requested": len(_TARGETS),
            "verified_in_memory": identity_verified,
        },
        "route": route,
        "trip_files_modified": False,
    }


def _route_failure_class(problems: tuple[object, ...]) -> str:
    """Expose only a stable, non-sensitive route failure category."""

    if len(problems) != 1:
        return "provider_unavailable"
    code = getattr(problems[0], "code", None)
    return {
        ProviderProblemCode.AUTH_FAILED: "auth_failed",
        ProviderProblemCode.QUOTA_EXHAUSTED: "quota_exhausted",
        ProviderProblemCode.RATE_LIMITED: "rate_limited",
        ProviderProblemCode.TIMEOUT: "timeout",
        ProviderProblemCode.NOT_FOUND: "not_found",
        ProviderProblemCode.INVALID_PROVIDER_REQUEST: "invalid_request",
        ProviderProblemCode.INVALID_PROVIDER_RESPONSE: "invalid_response",
        ProviderProblemCode.OUTSIDE_PROVIDER_HORIZON: "outside_provider_horizon",
        ProviderProblemCode.UNSUPPORTED_MODE: "unsupported_mode",
        ProviderProblemCode.EMPTY_RESPONSE: "empty_response",
        ProviderProblemCode.STALE_EVIDENCE: "stale_evidence",
        ProviderProblemCode.PROVIDER_UNAVAILABLE: "provider_unavailable",
    }.get(code, "provider_unavailable")


def _candidate_review_report(
    status: str,
    *,
    attempts: int = 0,
    review: PlaceIdentityReview | None = None,
    source_revision: str | None = None,
) -> dict[str, object]:
    """Return minimal ephemeral candidate-review material for one human."""

    result: dict[str, object] = {
        "contract_version": ISHIGAKI_PROVIDER_EXIT_GATE_VERSION,
        "status": status,
        "attempts": {"identity": attempts, "route": 0, "total": attempts},
        "trip_files_modified": False,
    }
    if review is None:
        return result
    if not isinstance(source_revision, str) or len(source_revision) != 64:
        raise ValueError("review output requires its private source revision")
    candidates: list[dict[str, object]] = []
    for index, assessment in enumerate(review.assessments):
        candidate = assessment.candidate
        candidates.append(
            {
                "choice": chr(ord("A") + index),
                "display_name": candidate.display_name,
                "formatted_address": candidate.formatted_address,
                "primary_type": candidate.primary_type,
                "eligible": assessment.eligible,
                "exact_name_match": assessment.exact_name_match,
                "rejection_codes": [
                    item.value for item in assessment.rejection_codes
                ],
            }
        )
    result["review"] = {
        "attributions": [{"label": "Google Maps", "uri": None}],
        "status": review.status.value,
        "selection_allowed": (
            review.status is PlaceIdentityReviewStatus.REVIEW_REQUIRED
            and any(assessment.eligible for assessment in review.assessments)
        ),
        "results_truncated": review.results_truncated,
        "review_binding": _review_binding(source_revision, review),
        "selection_binding_v2": _selection_binding_v2(
            source_revision,
            review,
        ),
        "expires_at": review.expires_at.isoformat(),
        "expected": {
            "name": review.request.intent.expected_name,
            "region_code": review.request.intent.region_code,
            "locality": review.request.intent.expected_locality,
        },
        "candidates": candidates,
    }
    return result


def _review_binding(
    source_revision: str,
    review: PlaceIdentityReview,
) -> str:
    """Retain the original volatile review binding for compatibility only."""

    digest = hashlib.sha256(_REVIEW_BINDING_DOMAIN)
    digest.update(source_revision.encode("ascii"))
    digest.update(review.request.intent.intent_id.encode("ascii"))
    digest.update(review.request.policy_registry_revision.encode("ascii"))
    digest.update(review.contract_version.encode("ascii"))
    digest.update(review.review_id.encode("ascii"))
    digest.update(review.candidate_set_digest.encode("ascii"))
    return digest.hexdigest()


def _selection_binding_v2(
    source_revision: str,
    review: PlaceIdentityReview,
) -> str:
    """Stable, source-bound confirmation token; intentionally excludes time."""

    digest = hashlib.sha256(_SELECTION_BINDING_V2_DOMAIN)
    digest.update(source_revision.encode("ascii"))
    digest.update(review.request.intent.intent_id.encode("ascii"))
    digest.update(review.request.policy_registry_revision.encode("ascii"))
    digest.update(review.request.provider_request.policy_id.encode("ascii"))
    digest.update(",".join(review.request.field_mask).encode("ascii"))
    digest.update(review.contract_version.encode("ascii"))
    digest.update(review.status.value.encode("ascii"))
    digest.update(str(review.results_truncated).encode("ascii"))
    digest.update(review.candidate_set_digest.encode("ascii"))
    return digest.hexdigest()


def _digest_text(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _choice(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if len(normalized) != 1 or not "A" <= normalized <= "E":
        return None
    return normalized


def _choice_assessment(
    review: PlaceIdentityReview,
    choice: str,
) -> PlaceIdentityCandidateAssessment | None:
    index = ord(choice) - ord("A")
    if not 0 <= index < len(review.assessments):
        return None
    return review.assessments[index]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the fixed, non-persistent Ishigaki provider exit gate."
    )
    parser.add_argument("trip_path", help="Ishigaki legacy trip directory or data directory")
    parser.add_argument(
        "--live",
        action="store_true",
        help="perform the bounded live provider calls (otherwise refuse)",
    )
    parser.add_argument(
        "--review-origin",
        action="store_true",
        help="run one source-bound origin candidate review without promotion",
    )
    parser.add_argument(
        "--minimal-route-diagnostic",
        action="store_true",
        help="test only whether Routes accepts the reduced diagnostic response mask",
    )
    parser.add_argument(
        "--undated-route-diagnostic",
        action="store_true",
        help="test the reduced mask without a planned departure time",
    )
    parser.add_argument(
        "--origin-choice",
        metavar="A-E",
        help="apply this turn's explicit user choice to a fresh origin review",
    )
    parser.add_argument(
        "--origin-selection-binding-v2",
        metavar="DIGEST",
        help="stable binding emitted by the matching read-only origin review",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=ISHIGAKI_PROVIDER_EXIT_GATE_VERSION,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.live:
        result = _report("live_opt_in_required")
        print(json.dumps(result, sort_keys=True))
        return 2
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        result = _report("api_key_unavailable")
        print(json.dumps(result, sort_keys=True))
        return 2
    transport = GoogleMapsHttpTransport(api_key)
    if arguments.review_origin:
        if (
            arguments.origin_choice is not None
            or arguments.origin_selection_binding_v2 is not None
            or arguments.minimal_route_diagnostic
            or arguments.undated_route_diagnostic
        ):
            result = _report("invalid_mode")
            print(json.dumps(result, sort_keys=True))
            return 2
        result = run_ishigaki_origin_candidate_review(
            arguments.trip_path,
            transport=transport,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "review_ready" else 2
    if (
        arguments.minimal_route_diagnostic
        or arguments.undated_route_diagnostic
    ):
        if (
            arguments.minimal_route_diagnostic
            and arguments.undated_route_diagnostic
        ):
            result = _report("invalid_mode")
            print(json.dumps(result, sort_keys=True))
            return 2
        result = run_ishigaki_minimal_route_diagnostic(
            arguments.trip_path,
            transport=transport,
            origin_choice=arguments.origin_choice,
            origin_selection_binding_v2=arguments.origin_selection_binding_v2,
            include_departure=not arguments.undated_route_diagnostic,
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "minimal_route_diagnostic_completed" else 2
    result = run_ishigaki_provider_exit_gate(
        arguments.trip_path,
        transport=transport,
        origin_choice=arguments.origin_choice,
        origin_selection_binding_v2=arguments.origin_selection_binding_v2,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "completed_memory_only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
