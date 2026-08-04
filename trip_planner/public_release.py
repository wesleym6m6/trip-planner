"""Fail-closed builder for explicitly approved public trip pages.

The legacy renderer remains a private, local preview: it deliberately carries
the complete seven-file workflow.  This module has a separate source tree and
never reads the project-local ``trips/`` tree.  A public release is therefore
possible only from a small, human-curated JSON document. Its exact bytes and
rendered HTML are bound in an explicit release manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from jinja2 import Environment, StrictUndefined, TemplateError, select_autoescape


PUBLIC_TRIP_SCHEMA_VERSION = "trip-planner.public-trip/v1"
PUBLIC_RELEASE_SCHEMA_VERSION = "trip-planner.public-release/v1"
PUBLIC_ARTIFACT_SCHEMA_VERSION = "trip-planner.public-artifacts/v1"

_MAX_SOURCE_BYTES = 128 * 1024
_MAX_TITLE_CHARS = 160
_MAX_DATE_LABEL_CHARS = 96
_MAX_CITY_COUNT = 8
_MAX_CITY_CHARS = 80
_MAX_DAY_COUNT = 31
_MAX_DAY_LABEL_CHARS = 160
_MAX_ITEM_COUNT = 32
_MAX_ITEM_TITLE_CHARS = 240
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TIME_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


class PublicReleaseError(ValueError):
    """One bounded public-release refusal with no source values attached."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,96}", code):
            raise ValueError("public release error code is invalid")
        self.code = code
        super().__init__(code)


class _JsonDecodeError(ValueError):
    """Internal strict-JSON sentinel that must not contain source text."""


@dataclass(frozen=True, slots=True)
class _PinnedDirectory:
    path: Path
    descriptor: int
    identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    directory: _PinnedDirectory
    name: str
    raw: bytes
    digest: str
    identity: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class PublicItem:
    time: str | None
    title: str


@dataclass(frozen=True, slots=True)
class PublicDay:
    day: int
    label: str
    items: tuple[PublicItem, ...]


@dataclass(frozen=True, slots=True)
class PublicTrip:
    slug: str
    title: str
    date_label: str
    cities: tuple[str, ...]
    days: tuple[PublicDay, ...]


@dataclass(frozen=True, slots=True)
class PublicReleaseEntry:
    slug: str
    public_json_sha256: str
    public_html_sha256: str


@dataclass(frozen=True, slots=True)
class _PublicRelease:
    entries: tuple[PublicReleaseEntry, ...]
    index_html_sha256: str
    snapshot: _FileSnapshot


@dataclass(frozen=True, slots=True)
class _LoadedPublicTrip:
    """Validated public source before it is rendered."""

    trip: PublicTrip
    snapshot: _FileSnapshot


@dataclass(frozen=True, slots=True)
class _RenderedPublicTrip:
    trip: PublicTrip
    source_snapshot: _FileSnapshot
    template_snapshot: _FileSnapshot
    html: str
    html_sha256: str


@dataclass(frozen=True, slots=True)
class _RenderedPublicTemplate:
    html: str
    snapshot: _FileSnapshot


def prepare_public_release_entry(
    slug: str,
    *,
    source_root: str | Path,
    template_dir: str | Path,
) -> dict[str, str]:
    """Return a deterministic candidate manifest entry without writing files."""

    candidate = prepare_public_release(
        (slug,), source_root=source_root, template_dir=template_dir
    )
    return candidate["trips"][0]


def prepare_public_release(
    slugs: Sequence[str],
    *,
    source_root: str | Path,
    template_dir: str | Path,
) -> dict[str, object]:
    """Return a complete no-write candidate public release manifest.

    The input ordering is intentional: it becomes the public index ordering and
    is therefore covered by the returned index-page digest.
    """

    if isinstance(slugs, (str, bytes)) or not isinstance(slugs, Sequence) or not slugs:
        raise PublicReleaseError("PUBLIC_RELEASE_ENTRIES_INVALID")
    safe_slugs: list[str] = []
    seen_slugs: set[str] = set()
    for slug in slugs:
        safe_slug = _validate_slug(slug)
        if safe_slug in seen_slugs:
            raise PublicReleaseError("PUBLIC_RELEASE_DUPLICATE_SLUG")
        safe_slugs.append(safe_slug)
        seen_slugs.add(safe_slug)

    source_root_path = _absolute_lexical_path(source_root)
    template_dir_path = _absolute_lexical_path(template_dir)
    with _pin_directory(source_root_path, "PUBLIC_SOURCE_ROOT_UNSAFE") as public_root:
        with _pin_child_directory(
            public_root,
            "trips",
            missing_code="PUBLIC_TRIP_SOURCE_MISSING",
            unsafe_code="PUBLIC_SOURCE_UNSAFE",
        ) as public_trips:
            with _pin_directory(
                template_dir_path, "PUBLIC_TEMPLATE_ROOT_UNSAFE"
            ) as public_templates:
                sources: list[_LoadedPublicTrip] = []
                rendered_templates: list[_RenderedPublicTemplate] = []
                entries: list[dict[str, str]] = []
                for safe_slug in safe_slugs:
                    source = _load_public_trip_source(safe_slug, public_trips)
                    rendered = _render_public_trip(source.trip, public_templates)
                    sources.append(source)
                    rendered_templates.append(rendered)
                    entries.append(
                        {
                            "slug": safe_slug,
                            "public_json_sha256": source.snapshot.digest,
                            "public_html_sha256": _sha256_text(rendered.html),
                        }
                    )
                index_rendered = _render_public_index(
                    tuple(source.trip for source in sources), public_templates
                )
                rendered_templates.append(index_rendered)
                for source in sources:
                    _assert_snapshot_current(source.snapshot)
                for rendered in rendered_templates:
                    _assert_snapshot_current(rendered.snapshot)
                _assert_pinned_child_directory_current(
                    public_root, "trips", public_trips
                )
                _assert_pinned_directory_current(public_root)
                _assert_pinned_directory_current(public_templates)
                return {
                    "schema_version": PUBLIC_RELEASE_SCHEMA_VERSION,
                    "public_index_html_sha256": _sha256_text(index_rendered.html),
                    "trips": entries,
                }


def build_public_site(
    manifest_path: str | Path,
    output_dir: str | Path,
    *,
    source_root: str | Path,
    template_dir: str | Path,
) -> dict[str, object]:
    """Build one exact public artifact tree from an approved release manifest.

    All parsing, digest verification, rendering, and source-drift checks finish
    before anything is written to ``output_dir``.  The caller should supply an
    empty temporary directory for output.
    """

    source_root_path = _absolute_lexical_path(source_root)
    template_dir_path = _absolute_lexical_path(template_dir)
    _require_release_manifest_path(
        _absolute_lexical_path(manifest_path), source_root_path
    )
    with _pin_directory(source_root_path, "PUBLIC_SOURCE_ROOT_UNSAFE") as public_root:
        with _pin_directory(
            template_dir_path, "PUBLIC_TEMPLATE_ROOT_UNSAFE"
        ) as public_templates:
            release = _load_release_manifest(public_root)
            with _pin_child_directory(
                public_root,
                "trips",
                missing_code="PUBLIC_TRIP_SOURCE_MISSING",
                unsafe_code="PUBLIC_SOURCE_UNSAFE",
            ) as public_trips:
                rendered: list[_RenderedPublicTrip] = []
                for entry in release.entries:
                    source = _load_public_trip_source(entry.slug, public_trips)
                    if source.snapshot.digest != entry.public_json_sha256:
                        raise PublicReleaseError("PUBLIC_SOURCE_DIGEST_MISMATCH")
                    trip_rendered = _render_public_trip(source.trip, public_templates)
                    html_sha256 = _sha256_text(trip_rendered.html)
                    if html_sha256 != entry.public_html_sha256:
                        raise PublicReleaseError("PUBLIC_HTML_DIGEST_MISMATCH")
                    rendered.append(
                        _RenderedPublicTrip(
                            trip=source.trip,
                            source_snapshot=source.snapshot,
                            template_snapshot=trip_rendered.snapshot,
                            html=trip_rendered.html,
                            html_sha256=html_sha256,
                        )
                    )

                index_rendered = _render_public_index(
                    tuple(item.trip for item in rendered), public_templates
                )
                if _sha256_text(index_rendered.html) != release.index_html_sha256:
                    raise PublicReleaseError("PUBLIC_INDEX_HTML_DIGEST_MISMATCH")
                _assert_snapshot_current(release.snapshot)
                for item in rendered:
                    _assert_snapshot_current(item.source_snapshot)
                for item in rendered:
                    _assert_snapshot_current(item.template_snapshot)
                _assert_snapshot_current(index_rendered.snapshot)
                _assert_pinned_child_directory_current(
                    public_root, "trips", public_trips
                )
                _assert_pinned_directory_current(public_root)
                _assert_pinned_directory_current(public_templates)

                output_path = _prepare_empty_output_directory(Path(output_dir))
                _write_text(output_path / "index.html", index_rendered.html)
                artifact_records = [
                    {
                        "path": "index.html",
                        "sha256": _sha256_text(index_rendered.html),
                    }
                ]
                for item in rendered:
                    relative_path = f"{item.trip.slug}/index.html"
                    _write_text(output_path / relative_path, item.html)
                    artifact_records.append(
                        {
                            "path": relative_path,
                            "sha256": item.html_sha256,
                        }
                    )
                release_manifest = {
                    "schema_version": PUBLIC_ARTIFACT_SCHEMA_VERSION,
                    "artifacts": artifact_records,
                }
                release_manifest_text = (
                    json.dumps(
                        release_manifest, ensure_ascii=False, sort_keys=True, indent=2
                    )
                    + "\n"
                )
                _write_text(output_path / "release-manifest.json", release_manifest_text)
                _assert_public_artifact_tree(
                    output_path,
                    {
                        "index.html",
                        "release-manifest.json",
                        *(record["path"] for record in artifact_records[1:]),
                    },
                )
                return {
                    "trip_count": len(rendered),
                    "artifact_count": len(artifact_records),
                    "release_manifest_sha256": _sha256_text(release_manifest_text),
                }


def _load_release_manifest(public_root: _PinnedDirectory) -> _PublicRelease:
    snapshot = _read_regular_snapshot(
        public_root, "release.json", "PUBLIC_RELEASE_MANIFEST_MISSING"
    )
    payload = _decode_json_object(snapshot.raw)
    _require_exact_keys(payload, {"schema_version", "public_index_html_sha256", "trips"})
    if payload.get("schema_version") != PUBLIC_RELEASE_SCHEMA_VERSION:
        raise PublicReleaseError("PUBLIC_RELEASE_SCHEMA_UNSUPPORTED")
    raw_entries = payload.get("trips")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise PublicReleaseError("PUBLIC_RELEASE_ENTRIES_INVALID")
    if len(raw_entries) > 64:
        raise PublicReleaseError("PUBLIC_RELEASE_ENTRIES_INVALID")
    index_html_sha256 = _validate_sha256(payload.get("public_index_html_sha256"))

    entries: list[PublicReleaseEntry] = []
    seen_slugs: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            raise PublicReleaseError("PUBLIC_RELEASE_ENTRY_INVALID")
        _require_exact_keys(
            raw_entry,
            {"slug", "public_json_sha256", "public_html_sha256"},
        )
        slug = _validate_slug(raw_entry.get("slug"))
        if slug in seen_slugs:
            raise PublicReleaseError("PUBLIC_RELEASE_DUPLICATE_SLUG")
        public_json_sha256 = _validate_sha256(raw_entry.get("public_json_sha256"))
        public_html_sha256 = _validate_sha256(raw_entry.get("public_html_sha256"))
        entries.append(
            PublicReleaseEntry(
                slug=slug,
                public_json_sha256=public_json_sha256,
                public_html_sha256=public_html_sha256,
            )
        )
        seen_slugs.add(slug)
    return _PublicRelease(
        entries=tuple(entries),
        index_html_sha256=index_html_sha256,
        snapshot=snapshot,
    )


def _load_public_trip_source(
    slug: str, public_trips: _PinnedDirectory
) -> _LoadedPublicTrip:
    snapshot = _read_regular_snapshot(
        public_trips, f"{slug}.json", "PUBLIC_TRIP_SOURCE_MISSING"
    )
    payload = _decode_json_object(snapshot.raw)
    _require_exact_keys(
        payload,
        {"schema_version", "slug", "title", "date_label", "cities", "days"},
    )
    if payload.get("schema_version") != PUBLIC_TRIP_SCHEMA_VERSION:
        raise PublicReleaseError("PUBLIC_TRIP_SCHEMA_UNSUPPORTED")
    if _validate_slug(payload.get("slug")) != slug:
        raise PublicReleaseError("PUBLIC_TRIP_SLUG_MISMATCH")
    trip = PublicTrip(
        slug=slug,
        title=_validate_text(payload.get("title"), _MAX_TITLE_CHARS),
        date_label=_validate_text(payload.get("date_label"), _MAX_DATE_LABEL_CHARS),
        cities=_parse_cities(payload.get("cities")),
        days=_parse_days(payload.get("days")),
    )
    return _LoadedPublicTrip(trip=trip, snapshot=snapshot)


def _parse_cities(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_CITY_COUNT:
        raise PublicReleaseError("PUBLIC_TRIP_CITIES_INVALID")
    cities = tuple(_validate_text(item, _MAX_CITY_CHARS) for item in value)
    if len(set(cities)) != len(cities):
        raise PublicReleaseError("PUBLIC_TRIP_CITIES_INVALID")
    return cities


def _parse_days(value: object) -> tuple[PublicDay, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_DAY_COUNT:
        raise PublicReleaseError("PUBLIC_TRIP_DAYS_INVALID")
    days: list[PublicDay] = []
    previous_day = 0
    for raw_day in value:
        if not isinstance(raw_day, Mapping):
            raise PublicReleaseError("PUBLIC_TRIP_DAY_INVALID")
        _require_exact_keys(raw_day, {"day", "label", "items"})
        day_number = raw_day.get("day")
        if type(day_number) is not int or not 1 <= day_number <= 366:
            raise PublicReleaseError("PUBLIC_TRIP_DAY_INVALID")
        if day_number <= previous_day:
            raise PublicReleaseError("PUBLIC_TRIP_DAYS_INVALID")
        previous_day = day_number
        raw_items = raw_day.get("items")
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= _MAX_ITEM_COUNT:
            raise PublicReleaseError("PUBLIC_TRIP_ITEMS_INVALID")
        items: list[PublicItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                raise PublicReleaseError("PUBLIC_TRIP_ITEM_INVALID")
            _require_exact_keys(raw_item, {"time", "title"})
            raw_time = raw_item.get("time")
            if raw_time is not None and (
                not isinstance(raw_time, str) or _TIME_RE.fullmatch(raw_time) is None
            ):
                raise PublicReleaseError("PUBLIC_TRIP_ITEM_INVALID")
            items.append(
                PublicItem(
                    time=raw_time,
                    title=_validate_text(raw_item.get("title"), _MAX_ITEM_TITLE_CHARS),
                )
            )
        days.append(
            PublicDay(
                day=day_number,
                label=_validate_text(raw_day.get("label"), _MAX_DAY_LABEL_CHARS),
                items=tuple(items),
            )
        )
    return tuple(days)


def _render_public_trip(
    trip: PublicTrip, template_dir: _PinnedDirectory
) -> _RenderedPublicTemplate:
    return _render_public_template("public_trip.html", template_dir, trip=trip)


def _render_public_index(
    trips: tuple[PublicTrip, ...], template_dir: _PinnedDirectory
) -> _RenderedPublicTemplate:
    return _render_public_template("public_index.html", template_dir, trips=trips)


def _render_public_template(
    template_name: str, template_dir: _PinnedDirectory, **context: object
) -> _RenderedPublicTemplate:
    snapshot = _read_regular_snapshot(
        template_dir,
        template_name,
        "PUBLIC_TEMPLATE_MISSING",
        unsafe_code="PUBLIC_TEMPLATE_UNSAFE",
        unavailable_code="PUBLIC_TEMPLATE_UNAVAILABLE",
    )
    try:
        source = snapshot.raw.decode("utf-8")
        environment = Environment(
            autoescape=select_autoescape(("html", "xml"), default_for_string=True),
            undefined=StrictUndefined,
        )
        html = environment.from_string(source).render(**context)
    except (UnicodeDecodeError, TemplateError, ValueError) as error:
        raise PublicReleaseError("PUBLIC_TEMPLATE_INVALID") from error
    return _RenderedPublicTemplate(html=html, snapshot=snapshot)


def _decode_json_object(raw: bytes) -> dict[str, Any]:
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_value,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _JsonDecodeError, ValueError) as error:
        if isinstance(error, PublicReleaseError):
            raise
        raise PublicReleaseError("PUBLIC_JSON_INVALID") from error
    if not isinstance(value, dict):
        raise PublicReleaseError("PUBLIC_JSON_INVALID")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _JsonDecodeError("duplicate key")
        result[key] = value
    return result


def _reject_nonfinite_json_value(_value: str) -> None:
    raise _JsonDecodeError("non-finite json value")


def _require_exact_keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise PublicReleaseError("PUBLIC_SCHEMA_FIELDS_INVALID")


def _validate_slug(value: object) -> str:
    if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
        raise PublicReleaseError("PUBLIC_SLUG_INVALID")
    return value


def _validate_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PublicReleaseError("PUBLIC_DIGEST_INVALID")
    return value


def _validate_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PublicReleaseError("PUBLIC_TEXT_INVALID")
    if any(
        ord(character) < 32
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise PublicReleaseError("PUBLIC_TEXT_INVALID")
    return value


def _read_regular_snapshot(
    directory: _PinnedDirectory,
    name: str,
    missing_code: str,
    *,
    unsafe_code: str = "PUBLIC_SOURCE_UNSAFE",
    unavailable_code: str = "PUBLIC_SOURCE_UNAVAILABLE",
) -> _FileSnapshot:
    try:
        first_stat = os.stat(
            name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise PublicReleaseError(missing_code) from error
    except OSError as error:
        raise PublicReleaseError(unavailable_code) from error
    if (
        stat.S_ISLNK(first_stat.st_mode)
        or not stat.S_ISREG(first_stat.st_mode)
        or first_stat.st_nlink != 1
    ):
        raise PublicReleaseError(unsafe_code)

    if not hasattr(os, "O_NOFOLLOW"):
        raise PublicReleaseError("PUBLIC_PLATFORM_UNSAFE")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory.descriptor)
    except FileNotFoundError as error:
        raise PublicReleaseError(missing_code) from error
    except OSError as error:
        raise PublicReleaseError(unsafe_code) from error
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink != 1:
            raise PublicReleaseError(unsafe_code)
        if (first_stat.st_dev, first_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            raise PublicReleaseError("PUBLIC_SOURCE_CHANGED")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            raw = handle.read(_MAX_SOURCE_BYTES + 1)
            after_stat = os.fstat(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > _MAX_SOURCE_BYTES:
        raise PublicReleaseError("PUBLIC_SOURCE_OVERSIZE")
    if _stat_identity(opened_stat) != _stat_identity(after_stat):
        raise PublicReleaseError("PUBLIC_SOURCE_CHANGED")
    return _FileSnapshot(
        directory=directory,
        name=name,
        raw=raw,
        digest=_sha256_bytes(raw),
        identity=_stat_identity(opened_stat),
    )


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _assert_snapshot_current(snapshot: _FileSnapshot) -> None:
    current = _read_regular_snapshot(
        snapshot.directory,
        snapshot.name,
        "PUBLIC_SOURCE_CHANGED",
    )
    if current.identity != snapshot.identity or current.digest != snapshot.digest:
        raise PublicReleaseError("PUBLIC_SOURCE_CHANGED")


def _require_safe_directory(path: Path, code: str) -> Path:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise PublicReleaseError(code) from error
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise PublicReleaseError(code)
    return path


def _absolute_lexical_path(value: str | Path) -> Path:
    """Normalize ``.`` and ``..`` without resolving any symlink."""

    return Path(os.path.abspath(value))


def _require_release_manifest_path(path: Path, source_root: Path) -> Path:
    """Keep the release decision inside the same explicit public namespace."""

    if path.name != "release.json" or path.parent != source_root:
        raise PublicReleaseError("PUBLIC_RELEASE_MANIFEST_UNSAFE")
    return path


def _directory_open_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise PublicReleaseError("PUBLIC_PLATFORM_UNSAFE")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def _pin_directory(path: Path, code: str) -> Iterator[_PinnedDirectory]:
    """Pin one direct input directory so path replacement cannot redirect reads."""

    try:
        first_stat = path.lstat()
    except OSError as error:
        raise PublicReleaseError(code) from error
    if stat.S_ISLNK(first_stat.st_mode) or not stat.S_ISDIR(first_stat.st_mode):
        raise PublicReleaseError(code)
    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError as error:
        raise PublicReleaseError(code) from error
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode):
            raise PublicReleaseError(code)
        if (first_stat.st_dev, first_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            raise PublicReleaseError("PUBLIC_SOURCE_CHANGED")
        yield _PinnedDirectory(
            path=path,
            descriptor=descriptor,
            identity=(opened_stat.st_dev, opened_stat.st_ino),
        )
    finally:
        os.close(descriptor)


@contextmanager
def _pin_child_directory(
    parent: _PinnedDirectory,
    name: str,
    *,
    missing_code: str,
    unsafe_code: str,
) -> Iterator[_PinnedDirectory]:
    """Pin one known child of an already pinned directory via openat."""

    try:
        first_stat = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise PublicReleaseError(missing_code) from error
    except OSError as error:
        raise PublicReleaseError(unsafe_code) from error
    if stat.S_ISLNK(first_stat.st_mode) or not stat.S_ISDIR(first_stat.st_mode):
        raise PublicReleaseError(unsafe_code)
    try:
        descriptor = os.open(
            name,
            _directory_open_flags(),
            dir_fd=parent.descriptor,
        )
    except FileNotFoundError as error:
        raise PublicReleaseError(missing_code) from error
    except OSError as error:
        raise PublicReleaseError(unsafe_code) from error
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode):
            raise PublicReleaseError(unsafe_code)
        if (first_stat.st_dev, first_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            raise PublicReleaseError("PUBLIC_SOURCE_CHANGED")
        yield _PinnedDirectory(
            path=parent.path / name,
            descriptor=descriptor,
            identity=(opened_stat.st_dev, opened_stat.st_ino),
        )
    finally:
        os.close(descriptor)


def _assert_pinned_directory_current(directory: _PinnedDirectory) -> None:
    try:
        current = directory.path.lstat()
    except OSError as error:
        raise PublicReleaseError("PUBLIC_SOURCE_CHANGED") from error
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != directory.identity
    ):
        raise PublicReleaseError("PUBLIC_SOURCE_CHANGED")


def _assert_pinned_child_directory_current(
    parent: _PinnedDirectory,
    name: str,
    directory: _PinnedDirectory,
) -> None:
    try:
        current = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise PublicReleaseError("PUBLIC_SOURCE_CHANGED") from error
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != directory.identity
    ):
        raise PublicReleaseError("PUBLIC_SOURCE_CHANGED")


def _prepare_empty_output_directory(path: Path) -> Path:
    if path.exists():
        output_dir = _require_safe_directory(path, "PUBLIC_OUTPUT_DIRECTORY_UNSAFE")
        try:
            next(output_dir.iterdir())
        except StopIteration:
            return output_dir
        raise PublicReleaseError("PUBLIC_OUTPUT_DIRECTORY_NOT_EMPTY")
    try:
        path.mkdir(parents=False)
    except OSError as error:
        raise PublicReleaseError("PUBLIC_OUTPUT_DIRECTORY_UNAVAILABLE") from error
    return path


def _write_text(path: Path, value: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError as error:
        raise PublicReleaseError("PUBLIC_OUTPUT_WRITE_FAILED") from error


def _assert_public_artifact_tree(output_dir: Path, expected_paths: set[str]) -> None:
    actual_paths: set[str] = set()
    for path in output_dir.rglob("*"):
        relative = path.relative_to(output_dir).as_posix()
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode):
            raise PublicReleaseError("PUBLIC_ARTIFACT_TREE_UNSAFE")
        if stat.S_ISDIR(path_stat.st_mode):
            continue
        if not stat.S_ISREG(path_stat.st_mode):
            raise PublicReleaseError("PUBLIC_ARTIFACT_TREE_UNSAFE")
        actual_paths.add(relative)
    if actual_paths != expected_paths:
        raise PublicReleaseError("PUBLIC_ARTIFACT_TREE_INVALID")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


__all__ = [
    "PUBLIC_ARTIFACT_SCHEMA_VERSION",
    "PUBLIC_RELEASE_SCHEMA_VERSION",
    "PUBLIC_TRIP_SCHEMA_VERSION",
    "PublicReleaseError",
    "build_public_site",
    "prepare_public_release",
    "prepare_public_release_entry",
]
