"""Safe, read-only Phase 5 command contracts.

``tripctl inspect`` deliberately starts with the established legacy evidence
preview rather than pretending that a legacy cache is canonical evidence.
``tripctl validate`` separately projects the deterministic planning kernel
from a bounded legacy source snapshot.  Neither command contacts providers,
loads an :class:`EvidenceStore` (whose retention read can write), migrates,
renders, or mutates trip files.

Canonical inspection is intentionally refused in this first slice.  A
canonical readiness result needs a trusted, exact runtime evidence snapshot;
falling back to adjacent legacy files or synthesising an empty snapshot would
misrepresent that contract.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Any

from .legacy_evidence import (
    LegacyEvidencePreviewError,
    preview_legacy_evidence,
    verify_legacy_evidence_source,
)
from .legacy_timeline import (
    LegacyTimelineValidationError,
    validate_legacy_timeline,
    verify_legacy_timeline_source,
)


TRIPCTL_VERSION = "tripctl/v1"
"""Version for the bounded public command envelope."""

_INSPECT_COMMAND = "inspect"
_VALIDATE_COMMAND = "validate"
_CANONICAL_INSPECT_UNAVAILABLE = "CANONICAL_INSPECT_UNAVAILABLE"
_CANONICAL_VALIDATE_UNAVAILABLE = "CANONICAL_VALIDATE_UNAVAILABLE"
_DATA_DIRECTORY_MISSING = "DATA_DIRECTORY_MISSING"
_DATA_DIRECTORY_UNSAFE = "DATA_DIRECTORY_UNSAFE"
_INSPECTION_UNAVAILABLE = "LEGACY_INSPECTION_UNAVAILABLE"
_VALIDATION_UNAVAILABLE = "LEGACY_TIMELINE_VALIDATION_UNAVAILABLE"
_PROBLEM_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_TIMELINE_REPAIR_CODES = frozenset(
    {
        "LEGACY_TIMELINE_SOURCE_UNAVAILABLE",
        "LEGACY_TIMELINE_SOURCE_MALFORMED",
        "LEGACY_TIMELINE_LOAD_FAILED",
    }
)


class TripctlError(ValueError):
    """One redacted command failure suitable for a JSON envelope."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        if (
            not isinstance(code, str)
            or _PROBLEM_CODE_RE.fullmatch(code) is None
        ):
            raise ValueError("TripctlError code must be a bounded token")
        if not isinstance(retryable, bool):
            raise TypeError("TripctlError retryable must be bool")
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def inspect_trip(path: str | Path) -> dict[str, Any]:
    """Return one redacted legacy inspection envelope.

    The caller receives no location, title, path, cache value, provider value,
    or raw identity.  A verifiable legacy preview is rechecked immediately
    before return.  An already unsafe or incomplete source remains a useful,
    non-retryable repair review instead of being mislabeled as a new drift.
    """

    data_dir = _data_dir(path)
    if _canonical_marker_present(data_dir):
        raise TripctlError(_CANONICAL_INSPECT_UNAVAILABLE)

    try:
        preview = preview_legacy_evidence(data_dir)
        if preview.source_verifiable:
            verify_legacy_evidence_source(preview)
    except LegacyEvidencePreviewError as exc:
        raise TripctlError(
            exc.code,
            retryable=exc.code == "STALE_LEGACY_EVIDENCE_PREVIEW",
        ) from exc
    except (
        MemoryError,
        OverflowError,
        RecursionError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise TripctlError(_INSPECTION_UNAVAILABLE) from exc
    if _canonical_marker_present(data_dir):
        raise TripctlError(_CANONICAL_INSPECT_UNAVAILABLE)

    preview_payload = preview.to_dict()
    source_verifiable = preview_payload["source_verifiable"]
    assert isinstance(source_verifiable, bool)
    return {
        "contract_version": TRIPCTL_VERSION,
        "command": _INSPECT_COMMAND,
        "ok": True,
        "status": (
            "review_required" if source_verifiable else "repair_required"
        ),
        "storage_mode": "legacy",
        "result": {
            "storage_mode": "legacy",
            "preview_digest": preview_payload["preview_digest"],
            "source_verifiable": preview_payload["source_verifiable"],
            "imports": preview_payload["imports"],
            "cleanup_targets": preview_payload["cleanup_targets"],
            "route_summary": preview_payload["route_summary"],
            "artifacts": preview_payload["artifacts"],
        },
        "problems": preview_payload["problems"],
        "retryable": False,
        "pending_review_retained": False,
        "next_action": (
            preview_payload["next_action"]
            if source_verifiable
            else "repair_source"
        ),
        "requires_user_review": preview_payload["requires_user_review"],
    }


def validate_trip(path: str | Path) -> dict[str, Any]:
    """Return a redacted, deterministic legacy timeline-validation envelope.

    This deliberately does *not* replace the existing seven-file renderer
    validator.  It adds the distinct planning-kernel question: what timeline
    blockers are currently known from the legacy trip/itinerary pair?  The
    pair is read through a bounded no-follow snapshot; providers, cache files,
    stores, rendering, and trip writes stay out of scope.
    """

    data_dir = _data_dir(path)
    if _canonical_marker_present(data_dir):
        raise TripctlError(_CANONICAL_VALIDATE_UNAVAILABLE)

    try:
        validation = validate_legacy_timeline(data_dir)
        verify_legacy_timeline_source(validation)
    except LegacyTimelineValidationError as exc:
        # A canonical marker always wins over adjacent legacy bytes, including
        # when it appears while a source repair result is being prepared.
        if _canonical_marker_present(data_dir):
            raise TripctlError(_CANONICAL_VALIDATE_UNAVAILABLE) from exc
        if exc.code == "STALE_LEGACY_TIMELINE_SOURCE":
            raise TripctlError(exc.code, retryable=True) from exc
        if exc.code in _TIMELINE_REPAIR_CODES:
            return _timeline_repair_result(exc.code, exc.affected_count)
        raise TripctlError(_VALIDATION_UNAVAILABLE) from exc
    except (
        MemoryError,
        OverflowError,
        RecursionError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise TripctlError(_VALIDATION_UNAVAILABLE) from exc

    if _canonical_marker_present(data_dir):
        raise TripctlError(_CANONICAL_VALIDATE_UNAVAILABLE)

    payload = validation.to_dict()
    timeline_status = payload["timeline_status"]
    assert isinstance(timeline_status, str)
    next_action, requires_user_review = _timeline_next_action(timeline_status)
    return {
        "contract_version": TRIPCTL_VERSION,
        "command": _VALIDATE_COMMAND,
        "ok": True,
        # Completion only means this narrow, two-file timeline projection was
        # evaluated.  It never promotes legacy data to travel-ready evidence
        # or replaces the seven-file renderer validator.
        "status": "review_required",
        "storage_mode": "legacy",
        "result": {
            "timeline_status": timeline_status,
            "day_count": payload["day_count"],
            "activity_count": payload["activity_count"],
            "timeline_entry_count": payload["timeline_entry_count"],
            "day_summary_count": payload["day_summary_count"],
        },
        "problems": payload["problems"],
        "retryable": False,
        "pending_review_retained": False,
        "next_action": next_action,
        "requires_user_review": requires_user_review,
    }


def inspection_failure(error: TripctlError) -> dict[str, Any]:
    """Return the backward-compatible safe ``inspect`` failure envelope."""

    return command_failure(_INSPECT_COMMAND, error)


def validation_failure(error: TripctlError) -> dict[str, Any]:
    """Return the safe ``validate`` failure envelope used by the CLI."""

    return command_failure(_VALIDATE_COMMAND, error)


def command_failure(command: str, error: TripctlError) -> dict[str, Any]:
    """Return one safe command-aware failure envelope."""

    if not isinstance(error, TripctlError):
        raise TypeError("error must be a TripctlError")
    safe_command = (
        command if command in {_INSPECT_COMMAND, _VALIDATE_COMMAND} else "unknown"
    )
    storage_mode = (
        "canonical"
        if error.code
        in {_CANONICAL_INSPECT_UNAVAILABLE, _CANONICAL_VALIDATE_UNAVAILABLE}
        else "unknown"
    )
    return {
        "contract_version": TRIPCTL_VERSION,
        "command": safe_command,
        "ok": False,
        "status": "rejected",
        "result": None,
        "problems": [{"code": error.code}],
        "retryable": error.retryable,
        "pending_review_retained": False,
        "next_action": _failure_next_action(error.code),
        "requires_user_review": False,
        "storage_mode": storage_mode,
    }


def _timeline_repair_result(code: str, affected_count: int) -> dict[str, Any]:
    """Keep source/load repair distinct from a rejected CLI operation."""

    if code not in _TIMELINE_REPAIR_CODES:
        raise ValueError("timeline repair code is unsupported")
    if type(affected_count) is not int or affected_count < 1:
        raise ValueError("timeline repair count must be positive")
    return {
        "contract_version": TRIPCTL_VERSION,
        "command": _VALIDATE_COMMAND,
        "ok": True,
        "status": "repair_required",
        "storage_mode": "legacy",
        "result": None,
        "problems": [{"code": code, "affected_count": affected_count}],
        "retryable": False,
        "pending_review_retained": False,
        "next_action": "repair_source",
        "requires_user_review": False,
    }


def _data_dir(path: str | Path) -> Path:
    try:
        candidate = Path(path)
    except (TypeError, ValueError) as exc:
        raise TripctlError(_DATA_DIRECTORY_MISSING) from exc
    try:
        if candidate.name == "data":
            data_dir = candidate
        elif (candidate / "data").is_dir():
            data_dir = candidate / "data"
        else:
            raise TripctlError(_DATA_DIRECTORY_MISSING)
        info = data_dir.stat()
    except OSError as exc:
        raise TripctlError(_DATA_DIRECTORY_MISSING) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise TripctlError(_DATA_DIRECTORY_MISSING)
    return data_dir


def _canonical_marker_present(data_dir: Path) -> bool:
    """Detect any canonical marker without reading or following it.

    A broken symlink, directory, FIFO, or ordinary ``plan.json`` all block
    legacy fallback.  The next canonical-specific interface must decide how
    it can inspect that object safely.
    """

    try:
        (data_dir / "plan.json").lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise TripctlError(_DATA_DIRECTORY_UNSAFE) from exc
    return True


def _failure_next_action(code: str) -> str:
    if code in {_CANONICAL_INSPECT_UNAVAILABLE, _CANONICAL_VALIDATE_UNAVAILABLE}:
        return "use_developer_runtime"
    if code == "STALE_LEGACY_EVIDENCE_PREVIEW":
        return "retry_inspection"
    if code == "STALE_LEGACY_TIMELINE_SOURCE":
        return "retry_validation"
    return "repair_source"


def _timeline_next_action(timeline_status: str) -> tuple[str, bool]:
    if timeline_status == "feasible":
        return "review_timeline", True
    if timeline_status == "infeasible":
        return "repair_timeline", True
    if timeline_status == "needs_verification":
        return "review_timeline", True
    raise TripctlError(_VALIDATION_UNAVAILABLE)


__all__ = [
    "TRIPCTL_VERSION",
    "TripctlError",
    "command_failure",
    "inspect_trip",
    "inspection_failure",
    "validate_trip",
    "validation_failure",
]
