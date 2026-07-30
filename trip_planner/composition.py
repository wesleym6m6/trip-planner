"""Pure runtime composition of canonical plans and provider evidence.

The canonical plan remains the only durable planning document.  This module
projects selected normalized route observations into an immutable
``TripState`` sidecar and carries the exact evidence identity plus live
attribution needed by later runtime consumers.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timezone
from typing import Any, Mapping

from .codec import plan_to_trip_state
from .availability import (
    ActivityAvailability,
    AvailabilityDisposition,
    AvailabilityInterval,
)
from .facts import EvidenceSnapshot, FactKey, FactKind, FactObservation
from .models import CheckIssue, EvidenceState, TravelEstimate, TripState


COMPOSITION_VERSION = "trip-composition/v1"


@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    """Redacted identity for the evidence used by one composition.

    ``snapshot_id`` and ``purge_checked_at`` remain useful diagnostics, but
    neither participates in ``binding_digest``.  A later compliance-clock
    check therefore cannot invalidate an otherwise identical semantic
    evidence view.
    """

    policy_registry_revision: str
    store_revision: str
    evidence_revision: str
    evaluation_at: datetime
    purge_checked_at: datetime
    snapshot_id: str
    used_observation_ids: tuple[str, ...] = ()
    required_attribution_labels: tuple[str, ...] = ()
    requires_live_attribution: bool = False
    outcome_revision: str | None = None
    binding_digest: str = ""
    contract_version: str = COMPOSITION_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != COMPOSITION_VERSION:
            raise ValueError(
                f"Unsupported composition version {self.contract_version!r}."
            )
        for value, name in (
            (self.policy_registry_revision, "policy_registry_revision"),
            (self.store_revision, "store_revision"),
            (self.evidence_revision, "evidence_revision"),
            (self.snapshot_id, "snapshot_id"),
        ):
            _require_digest(value, f"EvidenceBinding.{name}")
        evaluation_at = _aware_utc(
            self.evaluation_at, "EvidenceBinding.evaluation_at"
        )
        purge_checked_at = _aware_utc(
            self.purge_checked_at, "EvidenceBinding.purge_checked_at"
        )
        object.__setattr__(self, "evaluation_at", evaluation_at)
        object.__setattr__(self, "purge_checked_at", purge_checked_at)

        used_ids = _normalized_digests(
            self.used_observation_ids,
            "EvidenceBinding.used_observation_ids",
        )
        labels = _normalized_labels(self.required_attribution_labels)
        object.__setattr__(self, "used_observation_ids", used_ids)
        object.__setattr__(
            self, "required_attribution_labels", labels
        )
        if not isinstance(self.requires_live_attribution, bool):
            raise TypeError(
                "EvidenceBinding.requires_live_attribution must be bool"
            )
        if self.outcome_revision is not None:
            _require_digest(
                self.outcome_revision,
                "EvidenceBinding.outcome_revision",
            )

        expected = _binding_digest(
            policy_registry_revision=self.policy_registry_revision,
            store_revision=self.store_revision,
            evidence_revision=self.evidence_revision,
            evaluation_at=evaluation_at,
            outcome_revision=self.outcome_revision,
        )
        if self.binding_digest and self.binding_digest != expected:
            raise ValueError(
                "EvidenceBinding.binding_digest does not match its "
                "stable semantic identity."
            )
        object.__setattr__(self, "binding_digest", expected)

    def to_dict(self) -> dict[str, Any]:
        """Return only durable, redacted evidence identity."""

        result = {
            "contract_version": self.contract_version,
            "policy_registry_revision": self.policy_registry_revision,
            "store_revision": self.store_revision,
            "evidence_revision": self.evidence_revision,
            "evaluation_at": _utc_iso(self.evaluation_at),
            "purge_checked_at": _utc_iso(self.purge_checked_at),
            "snapshot_id": self.snapshot_id,
            "used_observation_ids": list(self.used_observation_ids),
            "required_attribution_labels": list(
                self.required_attribution_labels
            ),
            "requires_live_attribution": self.requires_live_attribution,
            "binding_digest": self.binding_digest,
        }
        if self.outcome_revision is not None:
            result["outcome_revision"] = self.outcome_revision
        return result


@dataclass(frozen=True, slots=True, repr=False)
class LiveAttribution:
    """Sanitized provider attribution that must remain process-local."""

    observation_id: str
    provider_id: str
    label: str = field(repr=False)
    uri: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_digest(
            self.observation_id, "LiveAttribution.observation_id"
        )
        _require_text(self.provider_id, "LiveAttribution.provider_id")
        _require_text(self.label, "LiveAttribution.label")
        if self.uri is not None:
            _require_text(self.uri, "LiveAttribution.uri")


@dataclass(frozen=True, slots=True)
class ComposedTripState:
    """Runtime state plus exact canonical and evidence bindings."""

    state: TripState = field(repr=False)
    trip_id: str
    plan_revision: str
    canonical_state_digest: str
    composed_state_digest: str
    evidence: EvidenceBinding
    live_attributions: tuple[LiveAttribution, ...] = field(
        default=(),
        repr=False,
    )
    activity_availability: tuple[ActivityAvailability, ...] = field(
        default=(),
        repr=False,
    )
    contract_version: str = COMPOSITION_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != COMPOSITION_VERSION:
            raise ValueError(
                f"Unsupported composition version {self.contract_version!r}."
            )
        if type(self.state) is not TripState:
            raise TypeError("ComposedTripState.state must be exact TripState")
        _require_identity(self.trip_id, "ComposedTripState.trip_id")
        _require_digest(self.plan_revision, "ComposedTripState.plan_revision")
        _require_state_digest(
            self.canonical_state_digest,
            "ComposedTripState.canonical_state_digest",
        )
        _require_state_digest(
            self.composed_state_digest,
            "ComposedTripState.composed_state_digest",
        )
        if self.state.revision != self.plan_revision:
            raise ValueError(
                "ComposedTripState plan revision differs from its state."
            )
        if _trip_state_digest(self.state) != self.composed_state_digest:
            raise ValueError(
                "ComposedTripState.composed_state_digest does not match state."
            )
        if type(self.evidence) is not EvidenceBinding:
            raise TypeError(
                "ComposedTripState.evidence must be exact EvidenceBinding"
            )
        if not isinstance(self.live_attributions, tuple) or any(
            type(item) is not LiveAttribution
            for item in self.live_attributions
        ):
            raise TypeError(
                "ComposedTripState.live_attributions must contain exact "
                "LiveAttribution values"
            )
        if not isinstance(self.activity_availability, tuple) or any(
            type(item) is not ActivityAvailability
            for item in self.activity_availability
        ):
            raise TypeError(
                "ComposedTripState.activity_availability must contain "
                "exact values"
            )
        if len(
            {
                item.activity_id
                for item in self.activity_availability
            }
        ) != len(self.activity_availability):
            raise ValueError(
                "ComposedTripState.activity_availability has duplicate "
                "activity IDs"
            )
        object.__setattr__(
            self,
            "activity_availability",
            tuple(
                sorted(
                    self.activity_availability,
                    key=lambda item: item.activity_id,
                )
            ),
        )
        normalized = tuple(
            sorted(
                set(self.live_attributions),
                key=lambda item: (
                    item.observation_id,
                    item.provider_id,
                    item.label,
                    item.uri or "",
                ),
            )
        )
        object.__setattr__(self, "live_attributions", normalized)
        if self.evidence.requires_live_attribution:
            live_labels = {item.label for item in normalized}
            missing_labels = set(
                self.evidence.required_attribution_labels
            ).difference(live_labels)
            attributed_observation_ids = {
                item.observation_id for item in normalized
            }
            missing_observations = set(
                self.evidence.used_observation_ids
            ).difference(attributed_observation_ids)
            if missing_labels or missing_observations:
                raise ValueError(
                    "Evidence requiring live attribution is missing "
                    "required labels or used-observation attribution."
                )

    @property
    def evidence_binding(self) -> EvidenceBinding:
        """Explicit alias for callers that prefer the contract name."""

        return self.evidence

    def to_dict(self) -> dict[str, Any]:
        """Return a durable binding without state or live provider content."""

        return {
            "contract_version": self.contract_version,
            "trip_id": self.trip_id,
            "plan_revision": self.plan_revision,
            "canonical_state_digest": self.canonical_state_digest,
            "composed_state_digest": self.composed_state_digest,
            "evidence": self.evidence.to_dict(),
        }


def compose_trip_state(
    canonical_plan: Mapping[str, Any],
    evidence_snapshot: EvidenceSnapshot,
    *,
    availability_keys: tuple[FactKey, ...] = (),
) -> ComposedTripState:
    """Compose one immutable, route-only runtime sidecar.

    Only fresh or stale, non-conflicted route resolutions are projected.
    Provider observations and canonical JSON are never mutated or serialized
    by this boundary.
    """

    if type(evidence_snapshot) is not EvidenceSnapshot:
        raise TypeError(
            "compose_trip_state requires an exact EvidenceSnapshot"
        )
    canonical_state = plan_to_trip_state(canonical_plan)
    trip_id = canonical_plan.get("trip_id")
    _require_identity(trip_id, "canonical plan trip_id")
    plan_revision = canonical_plan.get("revision")
    _require_digest(plan_revision, "canonical plan revision")
    canonical_digest = _trip_state_digest(canonical_state)

    field_names = {item.name for item in fields(TravelEstimate)}
    projections: dict[tuple[Any, ...], tuple[TravelEstimate, FactObservation]] = {}
    for key in _route_keys(evidence_snapshot):
        resolution = evidence_snapshot.resolve(key)
        if (
            resolution.evidence_state
            not in {EvidenceState.VERIFIED, EvidenceState.STALE}
            or resolution.selected is None
        ):
            continue
        projected = _project_route(
            resolution.selected,
            evidence_state=resolution.evidence_state,
            travel_field_names=field_names,
        )
        if projected is None:
            continue
        scope = _estimate_scope(projected, field_names)
        projections[scope] = (projected, resolution.selected)

    composed_estimates: list[TravelEstimate] = []
    matched_scopes: set[tuple[Any, ...]] = set()
    superseded_unverified_refs: set[str] = set()
    for estimate in canonical_state.travel_estimates:
        scope = _estimate_scope(estimate, field_names)
        projected_item = projections.get(scope)
        if projected_item is None:
            composed_estimates.append(estimate)
            continue
        projected, _observation = projected_item
        composed_estimates.append(
            _overlay_route(estimate, projected, field_names)
        )
        if (
            projected.evidence_state is EvidenceState.VERIFIED
            and estimate.evidence_state is not EvidenceState.VERIFIED
            and estimate.evidence_ref is not None
        ):
            superseded_unverified_refs.add(estimate.evidence_ref)
        matched_scopes.add(scope)
    for scope in sorted(
        set(projections).difference(matched_scopes),
        key=_scope_sort_key,
    ):
        composed_estimates.append(projections[scope][0])

    composed_state = replace(
        canonical_state,
        travel_estimates=tuple(composed_estimates),
        load_issues=_supersede_route_load_issues(
            canonical_state.load_issues,
            superseded_unverified_refs,
        ),
    )
    availability, availability_observations = project_activity_availability(
        canonical_state, evidence_snapshot, availability_keys
    )
    used_by_id = {
        observation.observation_id: observation
        for observation in (
            [
                observation
                for _projection, observation in projections.values()
            ]
            + list(availability_observations)
        )
    }
    used_observations = tuple(
        used_by_id[item_id] for item_id in sorted(used_by_id)
    )
    live_attributions = _live_attributions(used_observations)
    required_labels = tuple(
        sorted(
            {
                label
                for observation in used_observations
                for label in evidence_snapshot.policies.policy(
                    observation.provenance.retention_policy_id
                ).required_attribution_labels
            }
        )
    )
    binding = EvidenceBinding(
        policy_registry_revision=evidence_snapshot.policies.revision,
        store_revision=evidence_snapshot.store_revision,
        evidence_revision=evidence_snapshot.evidence_revision,
        evaluation_at=evidence_snapshot.evaluation_at,
        purge_checked_at=evidence_snapshot.purge_checked_at,
        snapshot_id=evidence_snapshot.snapshot_id,
        used_observation_ids=tuple(
            item.observation_id for item in used_observations
        ),
        required_attribution_labels=required_labels,
        requires_live_attribution=bool(
            required_labels or live_attributions
        ),
        outcome_revision=evidence_snapshot.outcome_revision,
    )
    return ComposedTripState(
        state=composed_state,
        trip_id=trip_id,
        plan_revision=plan_revision,
        canonical_state_digest=canonical_digest,
        composed_state_digest=_trip_state_digest(composed_state),
        evidence=binding,
        live_attributions=live_attributions,
        activity_availability=availability,
    )


def project_activity_availability(
    state: TripState,
    snapshot: EvidenceSnapshot,
    availability_keys: tuple[FactKey, ...] = (),
) -> tuple[tuple[ActivityAvailability, ...], tuple[FactObservation, ...]]:
    """Project exact-snapshot opening hours into runtime scheduling inputs."""

    if (
        not isinstance(availability_keys, tuple)
        or any(
            type(key) is not FactKey
            or key.kind is not FactKind.PLACE_OPENING_HOURS
            for key in availability_keys
        )
    ):
        raise TypeError(
            "availability_keys must contain exact opening-hours keys"
        )

    by_location: dict[str, list[Any]] = {}
    day_dates = {day.day_id: day.date for day in state.days}
    for activity in state.activities:
        by_location.setdefault(activity.location_id, []).append(activity)
    candidates: dict[str, list[ActivityAvailability]] = {}
    used: list[FactObservation] = []
    keys = {key.key_id: key for key in _hours_keys(snapshot)}
    keys.update({key.key_id: key for key in availability_keys})
    for key in (keys[key_id] for key_id in sorted(keys)):
        activities = by_location.get(key.subject_ids[0], [])
        if not activities:
            continue
        resolution = snapshot.resolve(key)
        qualifiers = key.qualifier_map
        target_start = date.fromisoformat(str(qualifiers["target_start"]))
        target_end = date.fromisoformat(str(qualifiers["target_end"]))
        relevant = [
            activity for activity in activities
            if target_start <= day_dates[activity.day_id] <= target_end
        ]
        if not relevant:
            continue
        used.extend(resolution.candidates)
        observation = resolution.selected
        if (
            observation is not None
            and resolution.supports_travel_ready_use
        ):
            payload = observation.value.payload
            disposition = AvailabilityDisposition.HARD_CURRENT
            candidate_intervals = tuple(
                AvailabilityInterval(
                    start_at=_parse_datetime(
                        item["start_at"],
                        "hours start_at",
                    ),
                    end_at=_parse_datetime(item["end_at"], "hours end_at"),
                )
                for item in payload["intervals"]
            )
            candidate_refs = (
                f"fact:{observation.observation_id}",
            )
            candidate_reason = None
            candidate_fresh_until = observation.valid_until
        else:
            disposition = AvailabilityDisposition.NEEDS_VERIFICATION
            candidate_intervals = ()
            candidate_refs = resolution.evidence_refs
            candidate_reason = {
                EvidenceState.VERIFIED: "regular_opening_hours",
                EvidenceState.STALE: "stale_opening_hours",
                EvidenceState.CONFLICTED: "conflicted_opening_hours",
                EvidenceState.UNVERIFIED: "missing_opening_hours",
            }[resolution.evidence_state]
            candidate_fresh_until = None
        for activity in relevant:
            candidates.setdefault(activity.activity_id, []).append(
                ActivityAvailability(
                    activity_id=activity.activity_id,
                    disposition=disposition,
                    intervals=candidate_intervals,
                    evidence_refs=candidate_refs,
                    reason=candidate_reason,
                    fresh_until=candidate_fresh_until,
                )
            )

    projected: list[ActivityAvailability] = []
    for activity_id in sorted(candidates):
        values = candidates[activity_id]
        hard = [
            item
            for item in values
            if item.disposition is AvailabilityDisposition.HARD_CURRENT
        ]
        semantic_hard: dict[
            tuple[tuple[datetime, datetime], ...],
            list[ActivityAvailability],
        ] = {}
        for item in hard:
            semantic_hard.setdefault(
                tuple(
                    (interval.start_at, interval.end_at)
                    for interval in item.intervals
                ),
                [],
            ).append(item)
        if len(semantic_hard) == 1:
            equivalent = next(iter(semantic_hard.values()))
            refs = tuple(
                sorted(
                    {
                        ref
                        for item in equivalent
                        for ref in item.evidence_refs
                    }
                )
            )
            fresh_until = min(
                item.fresh_until
                for item in equivalent
                if item.fresh_until is not None
            )
            projected.append(
                ActivityAvailability(
                    activity_id=activity_id,
                    disposition=AvailabilityDisposition.HARD_CURRENT,
                    intervals=equivalent[0].intervals,
                    evidence_refs=refs,
                    fresh_until=fresh_until,
                )
            )
        else:
            refs = tuple(
                sorted(
                    {
                        ref
                        for item in values
                        for ref in item.evidence_refs
                    }
                )
            )
            reason = (
                "conflicting_current_opening_hours"
                if len(semantic_hard) > 1
                else _needs_verification_reason(values)
            )
            projected.append(
                ActivityAvailability(
                    activity_id=activity_id,
                    disposition=AvailabilityDisposition.NEEDS_VERIFICATION,
                    evidence_refs=refs,
                    reason=reason,
                )
            )
    used_refs = {
        ref
        for availability in projected
        for ref in availability.evidence_refs
    }
    used_by_id = {
        item.observation_id: item
        for item in used
        if f"fact:{item.observation_id}" in used_refs
    }
    return (
        tuple(projected),
        tuple(used_by_id[item_id] for item_id in sorted(used_by_id)),
    )


def _needs_verification_reason(
    values: list[ActivityAvailability],
) -> str:
    precedence = (
        "conflicted_opening_hours",
        "stale_opening_hours",
        "regular_opening_hours",
        "missing_opening_hours",
    )
    reasons = {item.reason for item in values}
    return next(
        (reason for reason in precedence if reason in reasons),
        "missing_opening_hours",
    )


def _hours_keys(snapshot: EvidenceSnapshot) -> tuple[FactKey, ...]:
    by_id = {
        item.key.key_id: item.key
        for item in snapshot.observations
        if item.key.kind is FactKind.PLACE_OPENING_HOURS
    }
    return tuple(by_id[key_id] for key_id in sorted(by_id))


def _route_keys(snapshot: EvidenceSnapshot) -> tuple[Any, ...]:
    by_id = {
        observation.key.key_id: observation.key
        for observation in snapshot.observations
        if observation.key.kind is FactKind.ROUTE_ESTIMATE
    }
    return tuple(by_id[key_id] for key_id in sorted(by_id))


def _project_route(
    observation: FactObservation,
    *,
    evidence_state: EvidenceState,
    travel_field_names: set[str],
) -> TravelEstimate | None:
    qualifiers = observation.key.qualifier_map
    supported_qualifiers = {"mode"}
    kwargs: dict[str, Any] = {}
    timed_fields = (
        ("departure_at", "query_departure_at"),
        ("arrival_at", "query_arrival_at"),
    )
    for qualifier_name, estimate_name in timed_fields:
        if qualifier_name not in qualifiers:
            continue
        if estimate_name not in travel_field_names:
            return None
        supported_qualifiers.add(qualifier_name)
        kwargs[estimate_name] = _parse_datetime(
            qualifiers[qualifier_name],
            f"route qualifier {qualifier_name}",
        )
    if set(qualifiers).difference(supported_qualifiers):
        return None

    payload = observation.value.payload
    fact_ref = f"fact:{observation.observation_id}"
    kwargs.update(
        {
            "from_location_id": observation.key.subject_ids[0],
            "to_location_id": observation.key.subject_ids[1],
            "mode": payload["mode"],
            "duration_min": payload["duration_min"],
            "distance_km": payload.get("distance_km"),
            "static_duration_min": payload.get("static_duration_min"),
            "fallback_from_mode": payload.get("fallback_from_mode"),
            "warning_codes": tuple(payload.get("warning_codes", ())),
            "evidence_state": evidence_state,
            "fresh_until": observation.valid_until,
            "evidence_ref": fact_ref,
            "source": fact_ref,
        }
    )
    return TravelEstimate(**kwargs)


def _overlay_route(
    canonical: TravelEstimate,
    projected: TravelEstimate,
    travel_field_names: set[str],
) -> TravelEstimate:
    changes: dict[str, Any] = {
        "duration_min": projected.duration_min,
        "distance_km": projected.distance_km,
        "evidence_state": projected.evidence_state,
        "fresh_until": projected.fresh_until,
        "evidence_ref": projected.evidence_ref,
        "source": projected.source,
    }
    for name in (
        "query_departure_at",
        "query_arrival_at",
        "static_duration_min",
        "fallback_from_mode",
        "warning_codes",
    ):
        if name in travel_field_names:
            changes[name] = getattr(projected, name)
    return replace(canonical, **changes)


def _estimate_scope(
    estimate: TravelEstimate,
    travel_field_names: set[str],
) -> tuple[Any, ...]:
    result: list[Any] = [
        estimate.from_location_id,
        estimate.to_location_id,
        estimate.mode,
    ]
    for name in ("query_departure_at", "query_arrival_at"):
        if name in travel_field_names:
            value = getattr(estimate, name)
            result.append(None if value is None else _utc_iso(value))
    return tuple(result)


def _scope_sort_key(scope: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple("" if item is None else str(item) for item in scope)


def _live_attributions(
    observations: tuple[FactObservation, ...],
) -> tuple[LiveAttribution, ...]:
    return tuple(
        LiveAttribution(
            observation_id=observation.observation_id,
            provider_id=observation.provenance.provider_id,
            label=label,
            uri=uri,
        )
        for observation in observations
        for label, uri in observation.provenance.attributions
    )


def _trip_state_digest(state: TripState) -> str:
    """Resolve the scheduling digest lazily across the module boundary."""

    from .scheduling import trip_state_digest

    return trip_state_digest(state)


def _supersede_route_load_issues(
    issues: tuple[CheckIssue, ...],
    superseded_refs: set[str],
) -> tuple[CheckIssue, ...]:
    """Drop only loader warnings bound to exactly overlaid verified routes."""

    if not superseded_refs:
        return issues
    retained: list[CheckIssue] = []
    for issue in issues:
        if not _is_loader_unverified_route_summary(issue):
            retained.append(issue)
            continue
        remaining_refs = tuple(
            ref
            for ref in issue.evidence_refs
            if ref not in superseded_refs
        )
        if len(remaining_refs) == len(issue.evidence_refs):
            retained.append(issue)
            continue
        if not remaining_refs:
            continue
        count = len(remaining_refs)
        retained.append(
            replace(
                issue,
                message=(
                    f"{count} travel estimates lack an explicit verified "
                    "evidence state."
                ),
                evidence_refs=remaining_refs,
                details=(("count", count),),
            )
        )
    return tuple(retained)


def _is_loader_unverified_route_summary(issue: CheckIssue) -> bool:
    """Recognize the exact aggregate emitted by ``load_legacy_trip``."""

    return (
        issue.code == "TRAVEL_EVIDENCE_UNVERIFIED"
        and not issue.activity_ids
        and bool(issue.evidence_refs)
        and len(set(issue.evidence_refs)) == len(issue.evidence_refs)
        and issue.details == (("count", len(issue.evidence_refs)),)
        and issue.suggested_fixes == ("refresh_travel_estimates",)
    )


def _binding_digest(
    *,
    policy_registry_revision: str,
    store_revision: str,
    evidence_revision: str,
    evaluation_at: datetime,
    outcome_revision: str | None = None,
) -> str:
    payload = {
        "policy_registry_revision": policy_registry_revision,
        "store_revision": store_revision,
        "evidence_revision": evidence_revision,
        "evaluation_at": _utc_iso(evaluation_at),
    }
    if outcome_revision is not None:
        payload["outcome_revision"] = outcome_revision
    encoded = json.dumps(
        {
            "prefix": "evidence-binding",
            "payload": payload,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an RFC 3339 timestamp") from exc
    return _aware_utc(parsed, name)


def _aware_utc(value: Any, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise TypeError(f"{name} must be timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _normalized_digests(
    values: tuple[str, ...],
    name: str,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    for value in values:
        _require_digest(value, f"{name} item")
    return tuple(sorted(set(values)))


def _normalized_labels(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(
            "EvidenceBinding.required_attribution_labels must be a tuple"
        )
    for value in values:
        _require_text(
            value, "EvidenceBinding.required_attribution_labels item"
        )
    return tuple(sorted(set(values)))


def _require_digest(value: Any, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _require_state_digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{name} must be a stable state digest")
    _require_digest(value.removeprefix("sha256:"), name)


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")


def _require_identity(value: Any, name: str) -> None:
    _require_text(value, name)
    if (
        value != value.strip()
        or len(value) > 256
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise ValueError(
            f"{name} must be bounded visible text without surrounding space"
        )


__all__ = [
    "COMPOSITION_VERSION",
    "ComposedTripState",
    "EvidenceBinding",
    "LiveAttribution",
    "compose_trip_state",
    "project_activity_availability",
]
