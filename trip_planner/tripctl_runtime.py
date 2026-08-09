"""Exact, process-local runtime evidence composition for ``tripctl``.

Disk-only commands cannot establish current provider evidence.  This module is
the narrow host seam that accepts an already trusted :class:`EvidenceSnapshot`,
recomposes the exact canonical bytes, and projects the existing Phase 4.6
readiness contract without opening an ``EvidenceStore`` or serializing the
snapshot.  The private runtime inputs may be reused by the schedule facade, but
only the redacted assessment is public.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from .canonical_tripctl import (
    CanonicalTripctlError,
    _CanonicalSourceSnapshot,
    _read_source_snapshot,
    _source_is_current,
)
from .codec import PlanCodecError, decode_plan
from .composition import ComposedTripState, compose_trip_state
from .facts import EvidenceSnapshot, FactKey
from .lodging import LodgingIntakeAssessment
from .lodging_confirmation import LodgingConfirmationReview
from .readiness import TripReadiness, assess_trip_readiness


TRIPCTL_RUNTIME_VERSION = "tripctl-runtime/v1"
"""Version of the process-local evidence/readiness projection."""

_PROBLEM_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_SOURCE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_PLAIN_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class CanonicalRuntimeError(ValueError):
    """One bounded runtime-composition failure without private payloads."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        if not isinstance(code, str) or _PROBLEM_CODE_RE.fullmatch(code) is None:
            raise ValueError("canonical runtime error code must be bounded")
        if not isinstance(retryable, bool):
            raise TypeError("canonical runtime retryable must be bool")
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalRuntimeAssessment:
    """Safe readiness projection for one exact canonical/evidence binding."""

    source_digest: str
    plan_revision: str
    evidence_binding_ref: str
    readiness: TripReadiness = field(repr=False)
    lodging_intake_ref: str | None = field(default=None, repr=False)
    pending_lodging_review_ref: str | None = field(
        default=None,
        repr=False,
    )
    runtime_context_ref: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_digest, str)
            or _SOURCE_DIGEST_RE.fullmatch(self.source_digest) is None
        ):
            raise ValueError("source_digest must be a SHA-256 reference")
        for value, name in (
            (self.plan_revision, "plan_revision"),
            (self.evidence_binding_ref, "evidence_binding_ref"),
        ):
            if not isinstance(value, str) or _PLAIN_DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a plain SHA-256 digest")
        if type(self.readiness) is not TripReadiness:
            raise TypeError("readiness must be an exact TripReadiness")
        if self.readiness.plan_revision != self.plan_revision:
            raise ValueError("readiness plan revision does not match runtime source")
        if self.readiness.evidence_binding_digest != self.evidence_binding_ref:
            raise ValueError("readiness evidence binding does not match runtime source")
        for value, name in (
            (self.lodging_intake_ref, "lodging_intake_ref"),
            (self.pending_lodging_review_ref, "pending_lodging_review_ref"),
        ):
            if value is not None and _PLAIN_DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a plain SHA-256 digest")
        expected_context_ref = _runtime_context_ref(
            readiness_id=self.readiness.readiness_id,
            lodging_intake_ref=self.lodging_intake_ref,
            pending_lodging_review_ref=self.pending_lodging_review_ref,
        )
        if (
            self.runtime_context_ref
            and self.runtime_context_ref != expected_context_ref
        ):
            raise ValueError("runtime_context_ref differs from exact inputs")
        object.__setattr__(
            self,
            "runtime_context_ref",
            expected_context_ref,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": TRIPCTL_RUNTIME_VERSION,
            "source_digest": self.source_digest,
            "plan_revision": self.plan_revision,
            "evidence_binding_ref": self.evidence_binding_ref,
            "runtime_context_ref": self.runtime_context_ref,
            "runtime_evidence_loaded": True,
            "readiness_scope": "canonical",
            "readiness": self.readiness.to_dict(),
        }


@dataclass(frozen=True, slots=True, repr=False)
class _CanonicalRuntimeInputs:
    """Private exact inputs retained only within one host process."""

    source: _CanonicalSourceSnapshot = field(repr=False)
    plan: dict[str, Any] = field(repr=False)
    composed: ComposedTripState = field(repr=False)
    assessment: CanonicalRuntimeAssessment


def assess_canonical_runtime(
    path: str | Path,
    *,
    evidence_snapshot: EvidenceSnapshot,
    availability_keys: tuple[FactKey, ...] = (),
    lodging_intake: LodgingIntakeAssessment | None = None,
    pending_lodging_review: LodgingConfirmationReview | None = None,
) -> CanonicalRuntimeAssessment:
    """Project Phase 4.6 readiness from an exact injected snapshot."""

    return _load_canonical_runtime(
        Path(path),
        evidence_snapshot=evidence_snapshot,
        availability_keys=availability_keys,
        lodging_intake=lodging_intake,
        pending_lodging_review=pending_lodging_review,
    ).assessment


def _load_canonical_runtime(
    data_dir: Path,
    *,
    evidence_snapshot: EvidenceSnapshot,
    availability_keys: tuple[FactKey, ...] = (),
    lodging_intake: LodgingIntakeAssessment | None = None,
    pending_lodging_review: LodgingConfirmationReview | None = None,
) -> _CanonicalRuntimeInputs:
    if type(evidence_snapshot) is not EvidenceSnapshot:
        raise CanonicalRuntimeError("INVALID_RUNTIME_EVIDENCE")
    if (
        not isinstance(availability_keys, tuple)
        or any(type(item) is not FactKey for item in availability_keys)
    ):
        raise CanonicalRuntimeError("INVALID_RUNTIME_EVIDENCE")
    if (
        lodging_intake is not None
        and type(lodging_intake) is not LodgingIntakeAssessment
    ):
        raise CanonicalRuntimeError("INVALID_RUNTIME_EVIDENCE")
    if (
        pending_lodging_review is not None
        and type(pending_lodging_review) is not LodgingConfirmationReview
    ):
        raise CanonicalRuntimeError("INVALID_RUNTIME_EVIDENCE")

    try:
        source = _read_source_snapshot(data_dir)
    except CanonicalTripctlError as exc:
        raise CanonicalRuntimeError(
            exc.code,
            retryable=exc.retryable,
        ) from exc
    try:
        plan = decode_plan(source.raw)
        composed = compose_trip_state(
            plan,
            evidence_snapshot,
            availability_keys=availability_keys,
        )
        readiness = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=evidence_snapshot,
            availability_keys=availability_keys,
            lodging_intake=lodging_intake,
            pending_lodging_review=pending_lodging_review,
        )
        assessment = CanonicalRuntimeAssessment(
            source_digest=source.source_digest,
            plan_revision=str(plan["revision"]),
            evidence_binding_ref=composed.evidence.binding_digest,
            readiness=readiness,
            lodging_intake_ref=(
                lodging_intake.assessment_id
                if lodging_intake is not None
                else None
            ),
            pending_lodging_review_ref=(
                pending_lodging_review.review_id
                if pending_lodging_review is not None
                else None
            ),
        )
    except PlanCodecError:
        _raise_after_source_check(source, "CANONICAL_PLAN_MALFORMED")
    except (
        AssertionError,
        KeyError,
        MemoryError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        _raise_after_source_check(
            source,
            "RUNTIME_EVIDENCE_COMPOSITION_UNAVAILABLE",
        )
    except OSError:
        _raise_after_source_check(
            source,
            "RUNTIME_EVIDENCE_COMPOSITION_UNAVAILABLE",
        )
    inputs = _CanonicalRuntimeInputs(
        source=source,
        plan=plan,
        composed=composed,
        assessment=assessment,
    )
    _verify_runtime_source(inputs)
    return inputs


def _verify_runtime_source(inputs: _CanonicalRuntimeInputs) -> None:
    if type(inputs) is not _CanonicalRuntimeInputs:
        raise TypeError("inputs must be exact canonical runtime inputs")
    if not _source_is_current(inputs.source):
        raise CanonicalRuntimeError("STALE_CANONICAL_PLAN", retryable=True)


def _raise_after_source_check(
    source: _CanonicalSourceSnapshot,
    code: str,
) -> NoReturn:
    if not _source_is_current(source):
        raise CanonicalRuntimeError("STALE_CANONICAL_PLAN", retryable=True)
    raise CanonicalRuntimeError(code)


def _runtime_context_ref(
    *,
    readiness_id: str,
    lodging_intake_ref: str | None,
    pending_lodging_review_ref: str | None,
) -> str:
    if _PLAIN_DIGEST_RE.fullmatch(readiness_id) is None:
        raise ValueError("readiness_id must be a plain SHA-256 digest")
    payload = json.dumps(
        {
            "readiness_id": readiness_id,
            "lodging_intake_ref": lodging_intake_ref,
            "pending_lodging_review_ref": pending_lodging_review_ref,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(
        b"trip-planner.tripctl-runtime-context/v1\0" + payload
    ).hexdigest()


__all__ = [
    "TRIPCTL_RUNTIME_VERSION",
    "CanonicalRuntimeAssessment",
    "CanonicalRuntimeError",
    "assess_canonical_runtime",
]
