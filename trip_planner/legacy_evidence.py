"""Read-only Phase 4.6B preview for legacy provider-derived evidence.

The legacy trip workflow contains provider cache files and route estimates that
predate the evidence contract.  They have no trusted retrieval time, request
identity, policy binding, or retention proof, so this module can *only*
classify them.  It deliberately has no migration, cache import, cleanup, or
provider-call capability.

The preview keeps exact source fingerprints privately so a future trusted host
can reject a drifted review.  Its public representation is aggregate-only: it
never return place IDs, coordinates, cache values, URLs, prices, or raw trip
identities.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .codec import PlanCodecError, decode_json_bytes


LEGACY_EVIDENCE_PREVIEW_VERSION = "legacy-evidence-preview/v1"
MAX_LEGACY_EVIDENCE_FILE_BYTES = 16 * 1024 * 1024
_LEGACY_SOURCE_FILES = (
    "trip.json",
    "itinerary.json",
    "places_cache.json",
    "flights_cache.json",
    "hotels_cache.json",
)
_PRESENCE_ONLY_FILES = (
    "plan.json",
    ".trip-planner-evidence.json",
)
_MANIFEST_FILES = _LEGACY_SOURCE_FILES + _PRESENCE_ONLY_FILES
_HISTORY_DIRECTORY = ".trip-planner-history"
_SOURCE_DIGEST_DOMAIN = b"trip-planner.legacy-evidence-source/v1\0"
_PREVIEW_DIGEST_DOMAIN = b"trip-planner.legacy-evidence-preview/v1\0"
_UNKNOWN_CACHE_NAME_DOMAIN = b"trip-planner.legacy-evidence-unknown-cache/v1\0"


class LegacyEvidencePreviewError(ValueError):
    """A bounded, machine-readable legacy-preview input failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class LegacyEvidenceAction(str, Enum):
    """The only review actions suggested by the preview."""

    REVIEW_LEGACY_PREVIEW = "review_legacy_preview"
    REFRESH_PROVIDER_EVIDENCE = "refresh_provider_evidence"
    CLASSIFY_MANUAL_TRAVEL = "classify_manual_travel"
    REFRESH_PLACE_IDENTITY = "refresh_place_identity"
    REPAIR_LEGACY_SOURCE = "repair_legacy_source"
    KEEP_LEGACY_COMPATIBILITY = "keep_legacy_compatibility"


class LegacyEvidenceArtifactKind(str, Enum):
    """Known legacy artifacts; this is intentionally not a generic scanner."""

    PLACES_CACHE = "places_cache"
    FLIGHTS_CACHE = "flights_cache"
    HOTELS_CACHE = "hotels_cache"
    HISTORY = "history"


class LegacyEvidenceArtifactState(str, Enum):
    """Whether an artifact can be considered for any later cleanup review."""

    NOT_PRESENT = "not_present"
    QUARANTINED = "quarantined"
    COMPATIBILITY_BLOCKED = "compatibility_blocked"
    UNSAFE_SOURCE = "unsafe_source"
    OVERSIZED = "oversized"
    MALFORMED = "malformed"


class LegacyEvidenceSourceState(str, Enum):
    """Private manifest state used to reject an old preview after drift."""

    MISSING = "missing"
    PRESENT = "present"
    REGULAR_FILE = "regular_file"
    DIRECTORY = "directory"
    UNSAFE = "unsafe"
    OVERSIZED = "oversized"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class LegacyEvidenceProblem:
    """A redacted aggregate warning from one legacy evidence preview."""

    code: str
    action: LegacyEvidenceAction
    affected_count: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or not self.code
            or len(self.code) > 128
            or any(character.isspace() for character in self.code)
        ):
            raise ValueError("LegacyEvidenceProblem.code must be a bounded token")
        if type(self.affected_count) is not int or self.affected_count < 0:
            raise ValueError("LegacyEvidenceProblem.affected_count must be non-negative")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "code": self.code,
            "next_action": self.action.value,
            "affected_count": self.affected_count,
        }


@dataclass(frozen=True, slots=True, repr=False)
class _SourceFingerprint:
    """Exact, non-public source identity for a fixed manifest entry."""

    name: str
    state: LegacyEvidenceSourceState
    byte_count: int | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.state is LegacyEvidenceSourceState.REGULAR_FILE:
            if (
                type(self.byte_count) is not int
                or self.byte_count < 0
                or type(self.sha256) is not str
                or len(self.sha256) != 64
            ):
                raise ValueError("regular source fingerprint requires exact bytes")
        elif self.byte_count is not None or self.sha256 is not None:
            raise ValueError("non-file source fingerprints cannot carry bytes")

    def canonical_tuple(self) -> tuple[str, str, int | None, str | None]:
        return (self.name, self.state.value, self.byte_count, self.sha256)


@dataclass(frozen=True, slots=True)
class LegacyRouteSummary:
    """Counts only; no locations, modes, durations, or route values escape."""

    api_edge_count: int = 0
    manual_edge_count: int = 0
    unclassified_edge_count: int = 0
    place_id_count: int = 0
    coordinate_pair_count: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "api_edge_count",
            "manual_edge_count",
            "unclassified_edge_count",
            "place_id_count",
            "coordinate_pair_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "api_edge_count": self.api_edge_count,
            "manual_edge_count": self.manual_edge_count,
            "unclassified_edge_count": self.unclassified_edge_count,
            "place_id_count": self.place_id_count,
            "coordinate_pair_count": self.coordinate_pair_count,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LegacyEvidenceArtifact:
    """A known cache/history artifact with private exact source binding."""

    kind: LegacyEvidenceArtifactKind
    relative_path: str
    state: LegacyEvidenceArtifactState
    byte_count: int | None = None
    item_count: int | None = None
    _source: _SourceFingerprint | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.relative_path not in {
            "places_cache.json",
            "flights_cache.json",
            "hotels_cache.json",
            _HISTORY_DIRECTORY,
        }:
            raise ValueError("artifact path is outside the fixed legacy manifest")
        if self.byte_count is not None and (
            type(self.byte_count) is not int or self.byte_count < 0
        ):
            raise ValueError("artifact byte_count must be non-negative")
        if self.item_count is not None and (
            type(self.item_count) is not int or self.item_count < 0
        ):
            raise ValueError("artifact item_count must be non-negative")

    def to_dict(self) -> dict[str, str | int | None]:
        """Return metadata only; source digests and raw data stay private."""

        return {
            "kind": self.kind.value,
            "relative_path": self.relative_path,
            "state": self.state.value,
            "byte_count": self.byte_count,
            "item_count": self.item_count,
        }

    def __repr__(self) -> str:
        return (
            "LegacyEvidenceArtifact("
            f"kind={self.kind.value!r}, state={self.state.value!r}, "
            f"byte_count={self.byte_count!r}, item_count={self.item_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LegacyEvidencePreview:
    """Exact read-only preview that cannot authorize import, migration, or cleanup."""

    _data_dir: Path = field(repr=False)
    _source_revision: str = field(repr=False)
    preview_digest: str
    route_summary: LegacyRouteSummary
    artifacts: tuple[LegacyEvidenceArtifact, ...]
    problems: tuple[LegacyEvidenceProblem, ...]
    source_verifiable: bool
    _sources: tuple[_SourceFingerprint, ...] = field(repr=False)
    _unknown_cache_refs: tuple[str, ...] = field(repr=False, default=())

    def __post_init__(self) -> None:
        if (
            type(self._source_revision) is not str
            or len(self._source_revision) != 64
            or type(self.preview_digest) is not str
            or len(self.preview_digest) != 64
        ):
            raise ValueError("preview digests must be SHA-256 hex strings")
        if type(self.source_verifiable) is not bool:
            raise TypeError("source_verifiable must be bool")
        if tuple(sorted(source.name for source in self._sources)) != tuple(
            sorted((*_MANIFEST_FILES, _HISTORY_DIRECTORY))
        ):
            raise ValueError("preview must bind the complete fixed manifest")

    @property
    def cleanup_targets(self) -> tuple[object, ...]:
        """Always empty: Phase 4.6B never authorizes a destructive target."""

        return ()

    @property
    def import_count(self) -> int:
        """Always zero: old cache bytes can never bypass policy promotion."""

        return 0

    def source_is_current(self) -> bool:
        """Return whether the exact fixed manifest still matches this preview.

        This is a stale-preview guard only.  It does not authorize a later
        cleanup; an explicit user-reviewed, trusted-host operation would still
        need its own confirmation and filesystem checks.
        """

        if not self.source_verifiable:
            return False
        try:
            current_data_dir = _find_data_dir(self._data_dir)
        except LegacyEvidencePreviewError:
            return False
        try:
            if current_data_dir.resolve(strict=True) != self._data_dir.resolve(
                strict=True
            ):
                return False
        except OSError:
            return False
        current = _capture_sources(current_data_dir)
        if not _sources_are_verifiable(current):
            return False
        if tuple(item.canonical_tuple() for item in current) != tuple(
            item.canonical_tuple() for item in self._sources
        ):
            return False
        return _unknown_cache_refs(current_data_dir) == self._unknown_cache_refs

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, redacted review payload.

        The digest identifies an exact in-memory review without serializing
        cache bytes, source hashes, absolute paths, or raw trip identifiers.
        """

        return {
            "contract_version": LEGACY_EVIDENCE_PREVIEW_VERSION,
            "preview_digest": self.preview_digest,
            "source_verifiable": self.source_verifiable,
            "imports": self.import_count,
            "cleanup_targets": [],
            "route_summary": self.route_summary.to_dict(),
            "artifacts": [item.to_dict() for item in self.artifacts],
            "problems": [item.to_dict() for item in self.problems],
            "next_action": LegacyEvidenceAction.REVIEW_LEGACY_PREVIEW.value,
            "requires_user_review": True,
        }

    def __repr__(self) -> str:
        return (
            "LegacyEvidencePreview("
            f"preview_digest={self.preview_digest!r}, "
            f"api_edge_count={self.route_summary.api_edge_count!r}, "
            f"artifacts={len(self.artifacts)!r}, problems={len(self.problems)!r}, "
            f"source_verifiable={self.source_verifiable!r})"
        )


def preview_legacy_evidence(path: str | Path) -> LegacyEvidencePreview:
    """Inspect a fixed legacy-evidence manifest without changing any files.

    The function does not invoke migration preview because legacy evidence is
    not a canonical source of truth.  It also deliberately does not inspect
    unknown files or recursively walk history directories.
    """

    data_dir = _find_data_dir(Path(path))
    sources = _capture_sources(data_dir)
    by_name = {item.name: item for item in sources}
    source_verifiable = _preview_sources_are_verifiable(sources)

    problems: list[LegacyEvidenceProblem] = []
    artifacts: list[LegacyEvidenceArtifact] = []

    for required_name in ("trip.json", "itinerary.json"):
        source = by_name[required_name]
        if source.state is LegacyEvidenceSourceState.MISSING:
            problems.append(
                LegacyEvidenceProblem(
                    "LEGACY_EVIDENCE_SOURCE_MISSING",
                    LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
                )
            )
        elif source.state is not LegacyEvidenceSourceState.REGULAR_FILE:
            problems.append(
                LegacyEvidenceProblem(
                    "LEGACY_EVIDENCE_SOURCE_UNSAFE",
                    LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
                )
            )

    trip_value: Any | None = None
    trip_source = by_name["trip.json"]
    if trip_source.state is LegacyEvidenceSourceState.REGULAR_FILE:
        trip_value = _decode_source_json(data_dir, trip_source, problems)
    if trip_value is not None and not isinstance(trip_value, Mapping):
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_TRIP_MALFORMED",
                LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
            )
        )

    itinerary_value: Any | None = None
    itinerary_source = by_name["itinerary.json"]
    if itinerary_source.state is LegacyEvidenceSourceState.REGULAR_FILE:
        itinerary_value = _decode_source_json(data_dir, itinerary_source, problems)
    route_summary = _summarize_legacy_itinerary(itinerary_value, problems)
    _append_route_problems(route_summary, problems)

    for name, kind in (
        ("places_cache.json", LegacyEvidenceArtifactKind.PLACES_CACHE),
        ("flights_cache.json", LegacyEvidenceArtifactKind.FLIGHTS_CACHE),
        ("hotels_cache.json", LegacyEvidenceArtifactKind.HOTELS_CACHE),
    ):
        artifact, artifact_problem = _preview_cache_artifact(
            data_dir,
            by_name[name],
            kind,
        )
        artifacts.append(artifact)
        if artifact_problem is not None:
            problems.append(artifact_problem)

    history = by_name[_HISTORY_DIRECTORY]
    if history.state is LegacyEvidenceSourceState.DIRECTORY:
        artifacts.append(
            LegacyEvidenceArtifact(
                kind=LegacyEvidenceArtifactKind.HISTORY,
                relative_path=_HISTORY_DIRECTORY,
                state=LegacyEvidenceArtifactState.COMPATIBILITY_BLOCKED,
                _source=history,
            )
        )
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_HISTORY_CLEANUP_BLOCKED",
                LegacyEvidenceAction.KEEP_LEGACY_COMPATIBILITY,
            )
        )
    elif history.state is not LegacyEvidenceSourceState.MISSING:
        artifacts.append(
            LegacyEvidenceArtifact(
                kind=LegacyEvidenceArtifactKind.HISTORY,
                relative_path=_HISTORY_DIRECTORY,
                state=_artifact_state_for_source(history),
                _source=history,
            )
        )
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_HISTORY_SOURCE_UNSAFE",
                LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
            )
        )

    if by_name["plan.json"].state is not LegacyEvidenceSourceState.MISSING:
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_EVIDENCE_CLEANUP_BLOCKED_BY_CANONICAL_COMPATIBILITY",
                LegacyEvidenceAction.KEEP_LEGACY_COMPATIBILITY,
            )
        )
    if (
        by_name[".trip-planner-evidence.json"].state
        is not LegacyEvidenceSourceState.MISSING
    ):
        problems.append(
            LegacyEvidenceProblem(
                "CURRENT_EVIDENCE_STORE_OUT_OF_SCOPE",
                LegacyEvidenceAction.KEEP_LEGACY_COMPATIBILITY,
            )
        )

    unknown_cache_refs = _unknown_cache_refs(data_dir)
    if unknown_cache_refs:
        problems.append(
            LegacyEvidenceProblem(
                "UNKNOWN_LEGACY_CACHE_REVIEW_REQUIRED",
                LegacyEvidenceAction.REVIEW_LEGACY_PREVIEW,
                len(unknown_cache_refs),
            )
        )

    problems = _deduplicate_problems(problems)
    artifacts = sorted(artifacts, key=lambda item: item.relative_path)
    source_revision = _source_revision(sources, unknown_cache_refs)
    preview_digest = _preview_digest(
        source_revision,
        route_summary,
        artifacts,
        problems,
        source_verifiable,
        unknown_cache_refs,
    )
    return LegacyEvidencePreview(
        _data_dir=data_dir,
        _source_revision=source_revision,
        preview_digest=preview_digest,
        route_summary=route_summary,
        artifacts=tuple(artifacts),
        problems=tuple(problems),
        source_verifiable=source_verifiable,
        _sources=sources,
        _unknown_cache_refs=unknown_cache_refs,
    )


def verify_legacy_evidence_source(preview: LegacyEvidencePreview) -> None:
    """Fail closed when a preview's exact source manifest has changed."""

    if not isinstance(preview, LegacyEvidencePreview):
        raise TypeError("preview must be a LegacyEvidencePreview")
    if not preview.source_is_current():
        raise LegacyEvidencePreviewError(
            "STALE_LEGACY_EVIDENCE_PREVIEW",
            "legacy evidence source no longer matches its review preview",
        )


def _find_data_dir(path: Path) -> Path:
    candidate = path
    if candidate.name == "data":
        data_dir = candidate
    elif (candidate / "data").is_dir():
        data_dir = candidate / "data"
    else:
        data_dir = candidate
    try:
        status = data_dir.stat()
    except OSError as exc:
        raise LegacyEvidencePreviewError(
            "DATA_DIRECTORY_MISSING",
            "legacy evidence data directory is unavailable",
        ) from exc
    if not stat.S_ISDIR(status.st_mode):
        raise LegacyEvidencePreviewError(
            "DATA_DIRECTORY_MISSING",
            "legacy evidence input must resolve to a data directory",
        )
    return data_dir


def _capture_manifest_entry(data_dir: Path, name: str) -> _SourceFingerprint:
    path = data_dir / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _SourceFingerprint(name, LegacyEvidenceSourceState.MISSING)
    except OSError:
        return _SourceFingerprint(name, LegacyEvidenceSourceState.UNREADABLE)
    if stat.S_ISLNK(info.st_mode) or not (
        stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
    ):
        return _SourceFingerprint(name, LegacyEvidenceSourceState.UNSAFE)
    if stat.S_ISDIR(info.st_mode):
        return _SourceFingerprint(name, LegacyEvidenceSourceState.DIRECTORY)
    if info.st_size > MAX_LEGACY_EVIDENCE_FILE_BYTES:
        return _SourceFingerprint(name, LegacyEvidenceSourceState.OVERSIZED)
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError:
        return _SourceFingerprint(name, LegacyEvidenceSourceState.UNREADABLE)
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return _SourceFingerprint(name, LegacyEvidenceSourceState.UNSAFE)
            if opened.st_size > MAX_LEGACY_EVIDENCE_FILE_BYTES:
                return _SourceFingerprint(name, LegacyEvidenceSourceState.OVERSIZED)
            content = handle.read(opened.st_size + 1)
    except OSError:
        return _SourceFingerprint(name, LegacyEvidenceSourceState.UNREADABLE)
    if len(content) != info.st_size or opened.st_size != info.st_size:
        return _SourceFingerprint(name, LegacyEvidenceSourceState.UNREADABLE)
    return _SourceFingerprint(
        name,
        LegacyEvidenceSourceState.REGULAR_FILE,
        byte_count=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _capture_presence_entry(data_dir: Path, name: str) -> _SourceFingerprint:
    """Observe an out-of-scope path without opening, hashing, or following it."""

    try:
        (data_dir / name).lstat()
    except FileNotFoundError:
        return _SourceFingerprint(name, LegacyEvidenceSourceState.MISSING)
    except OSError:
        return _SourceFingerprint(name, LegacyEvidenceSourceState.UNREADABLE)
    return _SourceFingerprint(name, LegacyEvidenceSourceState.PRESENT)


def _capture_sources(data_dir: Path) -> tuple[_SourceFingerprint, ...]:
    return tuple(
        (
            *(
                _capture_manifest_entry(data_dir, name)
                for name in _LEGACY_SOURCE_FILES
            ),
            *(
                _capture_presence_entry(data_dir, name)
                for name in _PRESENCE_ONLY_FILES
            ),
            _capture_manifest_entry(data_dir, _HISTORY_DIRECTORY),
        )
    )


def _read_verified_source(data_dir: Path, source: _SourceFingerprint) -> bytes | None:
    """Read one already-fingerprinted source without following a later link.

    The preview first captures a fixed manifest and then needs the exact bytes
    from one of those entries.  Re-opening with :meth:`Path.read_bytes` would
    create a second, link-following read boundary.  Keep the same ``O_NOFOLLOW``
    and regular-file checks as manifest capture so every consumer of this
    helper (including future read-only projections) has one bounded source
    rule.
    """

    if source.state is not LegacyEvidenceSourceState.REGULAR_FILE:
        return None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(data_dir / source.name, flags)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return None
            if opened.st_size > MAX_LEGACY_EVIDENCE_FILE_BYTES:
                return None
            content = handle.read(opened.st_size + 1)
    except OSError:
        return None
    if (
        len(content) != opened.st_size
        or source.byte_count != len(content)
        or source.sha256 != hashlib.sha256(content).hexdigest()
    ):
        return None
    return content


def _decode_source_json(
    data_dir: Path,
    source: _SourceFingerprint,
    problems: list[LegacyEvidenceProblem],
) -> Any | None:
    content = _read_verified_source(data_dir, source)
    if content is None:
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_EVIDENCE_SOURCE_CHANGED",
                LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
            )
        )
        return None
    try:
        return decode_json_bytes(content)
    except (MemoryError, OverflowError, PlanCodecError, RecursionError, TypeError, ValueError):
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_EVIDENCE_JSON_MALFORMED",
                LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
            )
        )
        return None


def _summarize_legacy_itinerary(
    itinerary: Any | None,
    problems: list[LegacyEvidenceProblem],
) -> LegacyRouteSummary:
    if itinerary is None:
        return LegacyRouteSummary()
    if not isinstance(itinerary, Mapping):
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_ITINERARY_MALFORMED",
                LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
            )
        )
        return LegacyRouteSummary()
    days = itinerary.get("days")
    if not _is_sequence(days):
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_ITINERARY_MALFORMED",
                LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
            )
        )
        return LegacyRouteSummary()

    api_edges = 0
    manual_edges = 0
    unclassified_edges = 0
    place_ids = 0
    coordinates = 0
    malformed_days = 0
    for day in days:
        if not isinstance(day, Mapping):
            malformed_days += 1
            continue
        places = day.get("places", ())
        if not _is_sequence(places):
            malformed_days += 1
        else:
            for place in places:
                if not isinstance(place, Mapping):
                    malformed_days += 1
                    continue
                place_id = place.get("place_id")
                if isinstance(place_id, str) and place_id.strip():
                    place_ids += 1
                if _finite_number(place.get("lat")) and _finite_number(
                    place.get("lng")
                ):
                    coordinates += 1
        travel = day.get("travel", ())
        if not _is_sequence(travel):
            malformed_days += 1
            continue
        for edge in travel:
            if not isinstance(edge, Mapping):
                malformed_days += 1
                continue
            source = edge.get("source")
            if source == "api":
                api_edges += 1
            elif source == "manual":
                manual_edges += 1
            else:
                unclassified_edges += 1
    if malformed_days:
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_ITINERARY_PARTIAL",
                LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
                malformed_days,
            )
        )
    return LegacyRouteSummary(
        api_edge_count=api_edges,
        manual_edge_count=manual_edges,
        unclassified_edge_count=unclassified_edges,
        place_id_count=place_ids,
        coordinate_pair_count=coordinates,
    )


def _append_route_problems(
    summary: LegacyRouteSummary,
    problems: list[LegacyEvidenceProblem],
) -> None:
    if summary.api_edge_count:
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_PROVIDER_EVIDENCE_REFRESH_REQUIRED",
                LegacyEvidenceAction.REFRESH_PROVIDER_EVIDENCE,
                summary.api_edge_count,
            )
        )
    if summary.manual_edge_count:
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_MANUAL_TRAVEL_CLASSIFICATION_REQUIRED",
                LegacyEvidenceAction.CLASSIFY_MANUAL_TRAVEL,
                summary.manual_edge_count,
            )
        )
    if summary.unclassified_edge_count:
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_TRAVEL_PROVENANCE_UNKNOWN",
                LegacyEvidenceAction.CLASSIFY_MANUAL_TRAVEL,
                summary.unclassified_edge_count,
            )
        )
    if summary.place_id_count:
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_PLACE_IDENTITY_REFRESH_REQUIRED",
                LegacyEvidenceAction.REFRESH_PLACE_IDENTITY,
                summary.place_id_count,
            )
        )
    if summary.coordinate_pair_count:
        problems.append(
            LegacyEvidenceProblem(
                "LEGACY_COORDINATE_PROVENANCE_REQUIRED",
                LegacyEvidenceAction.CLASSIFY_MANUAL_TRAVEL,
                summary.coordinate_pair_count,
            )
        )


def _preview_cache_artifact(
    data_dir: Path,
    source: _SourceFingerprint,
    kind: LegacyEvidenceArtifactKind,
) -> tuple[LegacyEvidenceArtifact, LegacyEvidenceProblem | None]:
    if source.state is LegacyEvidenceSourceState.MISSING:
        missing_problem = (
            LegacyEvidenceProblem(
                "LEGACY_PLACE_CACHE_MISSING",
                LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
            )
            if kind is LegacyEvidenceArtifactKind.PLACES_CACHE
            else None
        )
        return (
            LegacyEvidenceArtifact(
                kind=kind,
                relative_path=source.name,
                state=LegacyEvidenceArtifactState.NOT_PRESENT,
                _source=source,
            ),
            missing_problem,
        )
    if source.state is not LegacyEvidenceSourceState.REGULAR_FILE:
        return (
            LegacyEvidenceArtifact(
                kind=kind,
                relative_path=source.name,
                state=_artifact_state_for_source(source),
                _source=source,
            ),
            LegacyEvidenceProblem(
                "LEGACY_CACHE_SOURCE_UNSAFE",
                LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
            ),
        )

    parsed = _decode_cache_json(data_dir, source)
    if parsed is None:
        return (
            LegacyEvidenceArtifact(
                kind=kind,
                relative_path=source.name,
                state=LegacyEvidenceArtifactState.MALFORMED,
                byte_count=source.byte_count,
                _source=source,
            ),
            LegacyEvidenceProblem(
                "LEGACY_CACHE_QUARANTINED",
                LegacyEvidenceAction.REPAIR_LEGACY_SOURCE,
            ),
        )
    item_count = len(parsed) if isinstance(parsed, (Mapping, list)) else None
    if kind is LegacyEvidenceArtifactKind.PLACES_CACHE:
        return (
            LegacyEvidenceArtifact(
                kind=kind,
                relative_path=source.name,
                state=LegacyEvidenceArtifactState.COMPATIBILITY_BLOCKED,
                byte_count=source.byte_count,
                item_count=item_count,
                _source=source,
            ),
            LegacyEvidenceProblem(
                "LEGACY_PLACE_CACHE_QUARANTINED",
                LegacyEvidenceAction.KEEP_LEGACY_COMPATIBILITY,
                1,
            ),
        )
    code = (
        "LEGACY_FLIGHT_CACHE_QUARANTINED"
        if kind is LegacyEvidenceArtifactKind.FLIGHTS_CACHE
        else "LEGACY_HOTEL_CACHE_QUARANTINED"
    )
    return (
        LegacyEvidenceArtifact(
            kind=kind,
            relative_path=source.name,
            state=LegacyEvidenceArtifactState.QUARANTINED,
            byte_count=source.byte_count,
            item_count=item_count,
            _source=source,
        ),
        LegacyEvidenceProblem(
            code,
            LegacyEvidenceAction.KEEP_LEGACY_COMPATIBILITY,
            1,
        ),
    )


def _decode_cache_json(data_dir: Path, source: _SourceFingerprint) -> Any | None:
    content = _read_verified_source(data_dir, source)
    if content is None:
        return None
    try:
        return decode_json_bytes(content)
    except (MemoryError, OverflowError, PlanCodecError, RecursionError, TypeError, ValueError):
        return None


def _artifact_state_for_source(
    source: _SourceFingerprint,
) -> LegacyEvidenceArtifactState:
    if source.state is LegacyEvidenceSourceState.OVERSIZED:
        return LegacyEvidenceArtifactState.OVERSIZED
    return LegacyEvidenceArtifactState.UNSAFE_SOURCE


def _unknown_cache_refs(data_dir: Path) -> tuple[str, ...]:
    """Record unknown names as opaque byte-derived refs, never as text."""

    try:
        children = tuple(data_dir.iterdir())
    except OSError:
        return ()
    known = {os.fsencode(name) for name in _MANIFEST_FILES}
    return tuple(
        sorted(
            hashlib.sha256(
                _UNKNOWN_CACHE_NAME_DOMAIN + os.fsencode(child.name)
            ).hexdigest()
            for child in children
            if os.fsencode(child.name).endswith(b"_cache.json")
            and os.fsencode(child.name) not in known
        )
    )


def _source_revision(
    sources: tuple[_SourceFingerprint, ...],
    unknown_cache_refs: tuple[str, ...],
) -> str:
    payload = {
        "sources": [source.canonical_tuple() for source in sources],
        "unknown_cache_refs": list(unknown_cache_refs),
    }
    return hashlib.sha256(
        _SOURCE_DIGEST_DOMAIN + _canonical_bytes(payload)
    ).hexdigest()


def _preview_digest(
    source_revision: str,
    route_summary: LegacyRouteSummary,
    artifacts: list[LegacyEvidenceArtifact],
    problems: list[LegacyEvidenceProblem],
    source_verifiable: bool,
    unknown_cache_refs: tuple[str, ...],
) -> str:
    payload = {
        "version": LEGACY_EVIDENCE_PREVIEW_VERSION,
        "source_revision": source_revision,
        "route_summary": route_summary.to_dict(),
        "artifacts": [item.to_dict() for item in artifacts],
        "problems": [item.to_dict() for item in problems],
        "source_verifiable": source_verifiable,
        "unknown_cache_count": len(unknown_cache_refs),
        "imports": 0,
        "cleanup_targets": 0,
    }
    return hashlib.sha256(
        _PREVIEW_DIGEST_DOMAIN + _canonical_bytes(payload)
    ).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _deduplicate_problems(
    problems: list[LegacyEvidenceProblem],
) -> list[LegacyEvidenceProblem]:
    grouped: dict[tuple[str, LegacyEvidenceAction], int] = {}
    for problem in problems:
        key = (problem.code, problem.action)
        grouped[key] = grouped.get(key, 0) + problem.affected_count
    return [
        LegacyEvidenceProblem(code, action, count)
        for (code, action), count in sorted(
            grouped.items(), key=lambda item: (item[0][0], item[0][1].value)
        )
    ]


def _sources_are_verifiable(
    sources: tuple[_SourceFingerprint, ...],
) -> bool:
    """A directory is intentionally unverifiable because we never recurse it."""

    return all(
        (
            item.state
            in {
                LegacyEvidenceSourceState.MISSING,
                LegacyEvidenceSourceState.PRESENT,
            }
            if item.name in _PRESENCE_ONLY_FILES
            else item.state
            in {
                LegacyEvidenceSourceState.MISSING,
                LegacyEvidenceSourceState.REGULAR_FILE,
            }
        )
        for item in sources
    )


def _preview_sources_are_verifiable(
    sources: tuple[_SourceFingerprint, ...],
) -> bool:
    """Require all legacy contract inputs before a preview can be reused."""

    if not _sources_are_verifiable(sources):
        return False
    by_name = {source.name: source for source in sources}
    return all(
        by_name[name].state is LegacyEvidenceSourceState.REGULAR_FILE
        for name in ("trip.json", "itinerary.json", "places_cache.json")
    )


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray, memoryview),
    )


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


__all__ = [
    "LEGACY_EVIDENCE_PREVIEW_VERSION",
    "MAX_LEGACY_EVIDENCE_FILE_BYTES",
    "LegacyEvidenceAction",
    "LegacyEvidenceArtifact",
    "LegacyEvidenceArtifactKind",
    "LegacyEvidenceArtifactState",
    "LegacyEvidencePreview",
    "LegacyEvidencePreviewError",
    "LegacyEvidenceProblem",
    "LegacyRouteSummary",
    "preview_legacy_evidence",
    "verify_legacy_evidence_source",
]
