#!/usr/bin/env python3
"""Maintain one private 24-hour provider credential session for local development.

The helper unlocks Bitwarden once, copies only allowlisted provider credentials
into the current user's private runtime tmpfs, and schedules generation-bound
expiry.  It never stores the Bitwarden session, prints credential values on a
safe command surface, calls a travel provider, or grants provider authority.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid


CONTRACT_VERSION = "trip-planner-dev-session/v1"
SESSION_TTL_SECONDS = 24 * 60 * 60
_RUNTIME_DIRECTORY_NAME = "trip-planner"
_SESSION_FILE_NAME = "provider-dev-session-v1.json"
_SESSION_LOCK_FILE_NAME = "provider-dev-session-v1.lock"
_MAX_SESSION_FILE_BYTES = 16 * 1024
_MAX_BITWARDEN_ITEM_LIST_BYTES = 1024 * 1024
_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_EXPECTED_RUNTIME_ROOT = Path(f"/run/user/{os.getuid()}")
_SYSTEMD_RUN_PATH = "/usr/bin/systemd-run"
_ALLOWED_CREDENTIAL_SLOTS = (
    "GOOGLE_MAPS_API_KEY",
    "SERPAPI_API_KEY",
)
_GOOGLE_API_KEY_RE = re.compile(r"AIza[A-Za-z0-9_-]{35}")
_OPAQUE_API_KEY_RE = re.compile(r"[A-Za-z0-9._-]{16,512}")
_GENERATION_RE = re.compile(r"[0-9a-f]{32}")
_BOOT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


class DevSessionError(RuntimeError):
    """Bounded failure that never contains credential material."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _BitwardenListResponseError(RuntimeError):
    """Private bounded-output failure with no response material."""


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    generation: str
    boot_id: str
    created_at_epoch: int
    expires_at_epoch: int
    created_boottime_ns: int
    expires_boottime_ns: int
    credentials: dict[str, str]


def _utc_text(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _safe_result(
    status: str,
    *,
    record: _SessionRecord | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "ttl_seconds": SESSION_TTL_SECONDS,
        "credential_values_exposed": False,
    }
    if record is not None:
        result.update(
            {
                "expires_at": _utc_text(record.expires_at_epoch),
                "credential_slots": sorted(record.credentials),
            }
        )
    return result


def _runtime_root(environ: Mapping[str, str]) -> Path:
    raw = environ.get("XDG_RUNTIME_DIR", "")
    if not raw:
        raise DevSessionError("runtime_dir_unavailable")
    root = Path(raw)
    if not root.is_absolute() or root != _EXPECTED_RUNTIME_ROOT:
        raise DevSessionError("runtime_dir_unsafe")
    try:
        info = root.lstat()
    except OSError as error:
        raise DevSessionError("runtime_dir_unavailable") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise DevSessionError("runtime_dir_unsafe")
    return root


def _session_directory(environ: Mapping[str, str], *, create: bool) -> Path:
    directory = _runtime_root(environ) / _RUNTIME_DIRECTORY_NAME
    if create:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise DevSessionError("session_dir_unavailable") from error
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if create:
            raise DevSessionError("session_dir_unavailable")
        return directory
    except OSError as error:
        raise DevSessionError("session_dir_unavailable") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DevSessionError("session_dir_unsafe")
    return directory


def session_path(environ: Mapping[str, str] = os.environ) -> Path:
    return _session_directory(environ, create=False) / _SESSION_FILE_NAME


def _current_boot_id() -> str:
    try:
        value = _BOOT_ID_PATH.read_text(encoding="ascii").strip().lower()
    except (OSError, UnicodeError) as error:
        raise DevSessionError("boot_identity_unavailable") from error
    if _BOOT_ID_RE.fullmatch(value) is None:
        raise DevSessionError("boot_identity_unavailable")
    return value


def _current_boottime_ns() -> int:
    if not hasattr(time, "CLOCK_BOOTTIME"):
        raise DevSessionError("boottime_unavailable")
    try:
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    except (OSError, ValueError) as error:
        raise DevSessionError("boottime_unavailable") from error


def _evaluated_clock(
    *,
    now_epoch: int | None,
    boottime_ns: int | None,
    boot_id: str | None,
) -> tuple[int, int, str]:
    wall = int(time.time()) if now_epoch is None else now_epoch
    monotonic = _current_boottime_ns() if boottime_ns is None else boottime_ns
    identity = _current_boot_id() if boot_id is None else boot_id
    if (
        type(wall) is not int
        or type(monotonic) is not int
        or wall < 0
        or monotonic < 0
        or not isinstance(identity, str)
        or _BOOT_ID_RE.fullmatch(identity) is None
    ):
        raise DevSessionError("clock_state_invalid")
    return wall, monotonic, identity


@contextmanager
def _session_lock(environ: Mapping[str, str]):
    directory = _session_directory(environ, create=True)
    path = directory / _SESSION_LOCK_FILE_NAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise DevSessionError("session_lock_unavailable") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise DevSessionError("session_lock_unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise DevSessionError("session_lock_unavailable") from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _validated_secret(slot: str, raw_value: str) -> str | None:
    if slot not in _ALLOWED_CREDENTIAL_SLOTS or not isinstance(raw_value, str):
        return None
    value = raw_value.strip()
    if not value or value == "null" or "\x00" in value:
        return None
    if slot == "GOOGLE_MAPS_API_KEY":
        candidates = sorted(set(_GOOGLE_API_KEY_RE.findall(value)))
        return candidates[0] if len(candidates) == 1 else None
    if _OPAQUE_API_KEY_RE.fullmatch(value) is None:
        return None
    return value


def _validated_record(payload: object) -> _SessionRecord:
    if not isinstance(payload, dict) or set(payload) != {
        "contract_version",
        "generation",
        "boot_id",
        "created_at_epoch",
        "expires_at_epoch",
        "created_boottime_ns",
        "expires_boottime_ns",
        "credentials",
    }:
        raise DevSessionError("session_record_invalid")
    if payload["contract_version"] != CONTRACT_VERSION:
        raise DevSessionError("session_record_invalid")
    generation = payload["generation"]
    boot_id = payload["boot_id"]
    created = payload["created_at_epoch"]
    expires = payload["expires_at_epoch"]
    created_boottime_ns = payload["created_boottime_ns"]
    expires_boottime_ns = payload["expires_boottime_ns"]
    raw_credentials = payload["credentials"]
    if (
        not isinstance(generation, str)
        or _GENERATION_RE.fullmatch(generation) is None
        or not isinstance(boot_id, str)
        or _BOOT_ID_RE.fullmatch(boot_id) is None
        or type(created) is not int
        or type(expires) is not int
        or type(created_boottime_ns) is not int
        or type(expires_boottime_ns) is not int
        or created < 0
        or created_boottime_ns < 0
        or expires - created != SESSION_TTL_SECONDS
        or expires_boottime_ns - created_boottime_ns
        != SESSION_TTL_SECONDS * 1_000_000_000
        or not isinstance(raw_credentials, dict)
        or not raw_credentials
        or not set(raw_credentials).issubset(_ALLOWED_CREDENTIAL_SLOTS)
        or "GOOGLE_MAPS_API_KEY" not in raw_credentials
    ):
        raise DevSessionError("session_record_invalid")
    credentials: dict[str, str] = {}
    for slot, raw_value in raw_credentials.items():
        normalized = _validated_secret(slot, raw_value)
        if normalized is None or normalized != raw_value:
            raise DevSessionError("session_record_invalid")
        credentials[slot] = normalized
    return _SessionRecord(
        generation=generation,
        boot_id=boot_id,
        created_at_epoch=created,
        expires_at_epoch=expires,
        created_boottime_ns=created_boottime_ns,
        expires_boottime_ns=expires_boottime_ns,
        credentials=credentials,
    )


def _read_record_unlocked(
    environ: Mapping[str, str],
    *,
    now_epoch: int,
    boottime_ns: int,
    boot_id: str,
    remove_expired: bool = True,
    enforce_lifetime: bool = True,
) -> _SessionRecord | None:
    path = session_path(environ)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DevSessionError("session_record_unreadable") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or before.st_size > _MAX_SESSION_FILE_BYTES
    ):
        raise DevSessionError("session_record_unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DevSessionError("session_record_unreadable") from error
    try:
        current = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise DevSessionError("session_record_changed")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            serialized = stream.read(_MAX_SESSION_FILE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(serialized.encode("utf-8")) > _MAX_SESSION_FILE_BYTES:
        raise DevSessionError("session_record_invalid")
    try:
        record = _validated_record(json.loads(serialized))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise DevSessionError("session_record_invalid") from error
    if enforce_lifetime and (
        boot_id != record.boot_id
        or now_epoch < record.created_at_epoch
        or boottime_ns < record.created_boottime_ns
        or now_epoch >= record.expires_at_epoch
        or boottime_ns >= record.expires_boottime_ns
    ):
        if remove_expired:
            _remove_generation_unlocked(environ, record.generation)
        return None
    return record


def _read_record(
    environ: Mapping[str, str],
    *,
    now_epoch: int,
    boottime_ns: int,
    boot_id: str,
    remove_expired: bool = True,
) -> _SessionRecord | None:
    with _session_lock(environ):
        return _read_record_unlocked(
            environ,
            now_epoch=now_epoch,
            boottime_ns=boottime_ns,
            boot_id=boot_id,
            remove_expired=remove_expired,
        )


def _write_record_unlocked(
    environ: Mapping[str, str],
    record: _SessionRecord,
) -> Path:
    directory = _session_directory(environ, create=True)
    path = directory / _SESSION_FILE_NAME
    temporary = directory / f".{_SESSION_FILE_NAME}.{record.generation}.tmp"
    payload = {
        "contract_version": CONTRACT_VERSION,
        "generation": record.generation,
        "boot_id": record.boot_id,
        "created_at_epoch": record.created_at_epoch,
        "expires_at_epoch": record.expires_at_epoch,
        "created_boottime_ns": record.created_boottime_ns,
        "expires_boottime_ns": record.expires_boottime_ns,
        "credentials": record.credentials,
    }
    serialized = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(serialized.encode("utf-8")) > _MAX_SESSION_FILE_BYTES:
        raise DevSessionError("session_record_invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise DevSessionError("session_record_unwritable") from error
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        _remove_generation_unlocked(environ, record.generation)
        raise DevSessionError("session_record_unsafe")
    return path


def _remove_generation_unlocked(
    environ: Mapping[str, str],
    generation: str,
) -> bool:
    try:
        record = _read_record_unlocked(
            environ,
            now_epoch=0,
            boottime_ns=0,
            boot_id="00000000-0000-0000-0000-000000000000",
            remove_expired=False,
            enforce_lifetime=False,
        )
    except DevSessionError:
        return False
    if record is None or record.generation != generation:
        return False
    path = session_path(environ)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise DevSessionError("session_record_unremovable") from error
    return True


def _remove_generation(environ: Mapping[str, str], generation: str) -> bool:
    with _session_lock(environ):
        return _remove_generation_unlocked(environ, generation)


def _remove_any_unlocked(environ: Mapping[str, str]) -> bool:
    path = session_path(environ)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise DevSessionError("session_record_unremovable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise DevSessionError("session_record_unsafe")
    try:
        path.unlink()
    except OSError as error:
        raise DevSessionError("session_record_unremovable") from error
    return True


def _remove_any(environ: Mapping[str, str]) -> bool:
    with _session_lock(environ):
        return _remove_any_unlocked(environ)


def _unlock_bitwarden_session(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    try:
        completed = run(
            ["bw", "unlock", "--raw"],
            stdout=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as error:
        raise DevSessionError("bitwarden_cli_unavailable") from error
    if completed.returncode != 0:
        raise DevSessionError("bitwarden_unlock_failed")
    session = completed.stdout.strip()
    if (
        not 20 <= len(session) <= 1024
        or any(character.isspace() for character in session)
    ):
        raise DevSessionError("bitwarden_session_invalid")
    return session


def _acquire_bitwarden_session(
    environ: Mapping[str, str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    inherited = environ.get("BW_SESSION", "").strip()
    if inherited and not any(character.isspace() for character in inherited):
        return inherited
    return _unlock_bitwarden_session(run=run)


def _run_bounded_bitwarden_item_list(
    command: list[str],
    child_environment: Mapping[str, str],
    *,
    max_stdout_bytes: int = _MAX_BITWARDEN_ITEM_LIST_BYTES,
) -> subprocess.CompletedProcess[str]:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            env=dict(child_environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise DevSessionError("bitwarden_cli_unavailable")
        serialized = process.stdout.read(max_stdout_bytes + 1)
        if len(serialized) > max_stdout_bytes:
            raise _BitwardenListResponseError()
        returncode = process.wait()
    except OSError as error:
        raise DevSessionError("bitwarden_cli_unavailable") from error
    finally:
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait()
                except OSError:
                    pass
    try:
        stdout = serialized.decode("utf-8")
    except UnicodeError as error:
        raise _BitwardenListResponseError() from error
    return subprocess.CompletedProcess(command, returncode, stdout=stdout)


def _read_bitwarden_credential(
    session: str,
    slot: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str | None:
    if slot not in _ALLOWED_CREDENTIAL_SLOTS:
        raise DevSessionError("credential_slot_invalid")

    def fail(reason: str) -> None:
        if slot == "GOOGLE_MAPS_API_KEY":
            raise DevSessionError(f"google_maps_credential_{reason}")

    child_environment = dict(os.environ)
    child_environment["BW_SESSION"] = session
    for option in (
        "BW_CLIENTID",
        "BW_CLIENTSECRET",
        "BW_CLEANEXIT",
        "BW_NOINTERACTION",
        "BW_PASSWORD",
        "BW_PRETTY",
        "BW_QUIET",
        "BW_RAW",
        "BW_RESPONSE",
        "GOOGLE_MAPS_API_KEY",
        "SERPAPI_API_KEY",
        "TRIP_PLANNER_DIRENV_BRIDGE",
    ):
        child_environment.pop(option, None)

    # Bitwarden treats a non-UUID `bw get` argument as a fuzzy search and
    # rejects multiple results.  Resolve the one exact canonical item name
    # ourselves so a similarly named vault item cannot select or block a
    # provider credential.  The private list response is never logged or
    # returned from this helper.
    try:
        command = [
            "bw",
            "list",
            "items",
            "--search",
            slot,
            "--nointeraction",
        ]
        if run is None:
            completed = _run_bounded_bitwarden_item_list(
                command,
                child_environment,
            )
        else:
            completed = run(
                command,
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
    except OSError as error:
        if slot == "GOOGLE_MAPS_API_KEY":
            raise DevSessionError("bitwarden_cli_unavailable") from error
        return None
    except DevSessionError as error:
        if slot != "GOOGLE_MAPS_API_KEY" and error.code == "bitwarden_cli_unavailable":
            return None
        raise
    except (_BitwardenListResponseError, UnicodeError):
        fail("response_invalid")
        return None
    if completed.returncode != 0:
        fail("lookup_failed")
        return None
    if not isinstance(completed.stdout, str):
        fail("response_invalid")
        return None
    try:
        serialized_size = len(completed.stdout.encode("utf-8"))
    except UnicodeError:
        fail("response_invalid")
        return None
    if serialized_size > _MAX_BITWARDEN_ITEM_LIST_BYTES:
        fail("response_invalid")
        return None
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, UnicodeError):
        fail("response_invalid")
        return None
    if not isinstance(payload, list) or any(
        not isinstance(item, dict) for item in payload
    ):
        fail("response_invalid")
        return None

    exact_items = [item for item in payload if item.get("name") == slot]
    if not exact_items:
        fail("item_not_found")
        return None
    if len(exact_items) != 1:
        fail("item_ambiguous")
        return None

    item = exact_items[0]
    raw_candidates: list[str] = []
    notes = item.get("notes")
    if isinstance(notes, str) and notes.strip():
        raw_candidates.append(notes)
    login = item.get("login")
    if isinstance(login, dict):
        password = login.get("password")
        if isinstance(password, str) and password.strip():
            raw_candidates.append(password)

    normalized_candidates = {
        normalized
        for raw_value in raw_candidates
        if (normalized := _validated_secret(slot, raw_value)) is not None
    }
    if len(normalized_candidates) != 1:
        fail("format_invalid")
        return None
    return normalized_candidates.pop()


def _schedule_expiry(
    generation: str,
    expires_at_epoch: int,
    environ: Mapping[str, str] = os.environ,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if (
        _GENERATION_RE.fullmatch(generation) is None
        or type(expires_at_epoch) is not int
        or expires_at_epoch < 0
    ):
        raise DevSessionError("expiry_timer_invalid")
    runtime_root = _runtime_root(environ)
    calendar_time = datetime.fromtimestamp(
        expires_at_epoch,
        timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S UTC")
    unit = f"trip-planner-dev-session-expiry-{generation[:12]}"
    command = [
        _SYSTEMD_RUN_PATH,
        "--user",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        f"--on-calendar={calendar_time}",
        "--timer-property=AccuracySec=1s",
        "--property=UnsetEnvironment=BW_SESSION GOOGLE_MAPS_API_KEY SERPAPI_API_KEY TRIP_PLANNER_DIRENV_BRIDGE",
        f"--setenv=XDG_RUNTIME_DIR={runtime_root}",
        sys.executable,
        str(Path(__file__).resolve()),
        "_expire",
        "--generation",
        generation,
    ]
    try:
        safe_environment = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": str(runtime_root),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_root / 'bus'}",
        }
        completed = run(
            command,
            env=safe_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as error:
        raise DevSessionError("expiry_timer_unavailable") from error
    if completed.returncode != 0:
        raise DevSessionError("expiry_timer_unavailable")


def start_daily_session(
    environ: Mapping[str, str] = os.environ,
    *,
    now_epoch: int | None = None,
    boottime_ns: int | None = None,
    boot_id: str | None = None,
    acquire_session: Callable[[Mapping[str, str]], str] | None = None,
    read_credential: Callable[[str, str], str | None] | None = None,
    schedule_expiry: Callable[[str, int], None] | None = None,
) -> dict[str, object]:
    session_loader = acquire_session or _acquire_bitwarden_session
    credential_loader = read_credential or _read_bitwarden_credential

    def load_credentials(session: str) -> dict[str, str]:
        loaded: dict[str, str] = {}
        for slot in _ALLOWED_CREDENTIAL_SLOTS:
            value = credential_loader(session, slot)
            normalized = _validated_secret(slot, value) if value is not None else None
            if normalized is not None:
                loaded[slot] = normalized
        return loaded

    with _session_lock(environ):
        checked_wall, checked_boot, checked_identity = _evaluated_clock(
            now_epoch=now_epoch,
            boottime_ns=boottime_ns,
            boot_id=boot_id,
        )
        current = _read_record_unlocked(
            environ,
            now_epoch=checked_wall,
            boottime_ns=checked_boot,
            boot_id=checked_identity,
        )
        if current is not None:
            return _safe_result("already_active", record=current)

        inherited = environ.get("BW_SESSION", "").strip()
        inherited_is_candidate = bool(inherited) and not any(
            character.isspace() for character in inherited
        )
        session = session_loader(environ)
        try:
            try:
                credentials = load_credentials(session)
            except DevSessionError as error:
                if not (
                    error.code == "google_maps_credential_lookup_failed"
                    and acquire_session is None
                    and inherited_is_candidate
                ):
                    raise
                credentials = {}
            if (
                "GOOGLE_MAPS_API_KEY" not in credentials
                and acquire_session is None
                and inherited_is_candidate
            ):
                session = ""
                session = _unlock_bitwarden_session()
                credentials = load_credentials(session)
        finally:
            session = ""
        if "GOOGLE_MAPS_API_KEY" not in credentials:
            raise DevSessionError("google_maps_credential_unavailable")

        created_wall, created_boot, created_identity = _evaluated_clock(
            now_epoch=now_epoch,
            boottime_ns=boottime_ns,
            boot_id=boot_id,
        )
        record = _SessionRecord(
            generation=uuid.uuid4().hex,
            boot_id=created_identity,
            created_at_epoch=created_wall,
            expires_at_epoch=created_wall + SESSION_TTL_SECONDS,
            created_boottime_ns=created_boot,
            expires_boottime_ns=(
                created_boot + SESSION_TTL_SECONDS * 1_000_000_000
            ),
            credentials=credentials,
        )
        _write_record_unlocked(environ, record)
        try:
            if schedule_expiry is None:
                _schedule_expiry(
                    record.generation,
                    record.expires_at_epoch,
                    environ,
                )
            else:
                schedule_expiry(record.generation, record.expires_at_epoch)
        except Exception:
            _remove_generation_unlocked(environ, record.generation)
            raise
        return _safe_result("started", record=record)


def session_status(
    environ: Mapping[str, str] = os.environ,
    *,
    now_epoch: int | None = None,
    boottime_ns: int | None = None,
    boot_id: str | None = None,
) -> dict[str, object]:
    with _session_lock(environ):
        wall, monotonic, identity = _evaluated_clock(
            now_epoch=now_epoch,
            boottime_ns=boottime_ns,
            boot_id=boot_id,
        )
        record = _read_record_unlocked(
            environ,
            now_epoch=wall,
            boottime_ns=monotonic,
            boot_id=identity,
        )
        if record is None:
            return _safe_result("inactive")
        return _safe_result("active", record=record)


def emit_credential(
    slot: str,
    environ: Mapping[str, str] = os.environ,
    *,
    now_epoch: int | None = None,
    boottime_ns: int | None = None,
    boot_id: str | None = None,
) -> str:
    if environ.get("TRIP_PLANNER_DIRENV_BRIDGE") != "1":
        raise DevSessionError("direnv_bridge_required")
    if slot not in _ALLOWED_CREDENTIAL_SLOTS:
        raise DevSessionError("credential_slot_invalid")
    with _session_lock(environ):
        wall, monotonic, identity = _evaluated_clock(
            now_epoch=now_epoch,
            boottime_ns=boottime_ns,
            boot_id=boot_id,
        )
        record = _read_record_unlocked(
            environ,
            now_epoch=wall,
            boottime_ns=monotonic,
            boot_id=identity,
        )
        if record is None or slot not in record.credentials:
            raise DevSessionError("credential_slot_unavailable")
        return record.credentials[slot]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the private 24-hour Trip Planner dev credential session."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start", help="unlock once and start or reuse today's session")
    commands.add_parser("status", help="show only safe session availability metadata")
    commands.add_parser("stop", help="remove cached provider credentials now")
    commands.add_parser("path", help="print the non-secret runtime session path")
    emit = commands.add_parser("_emit", help=argparse.SUPPRESS)
    emit.add_argument("slot", choices=_ALLOWED_CREDENTIAL_SLOTS)
    expire = commands.add_parser("_expire", help=argparse.SUPPRESS)
    expire.add_argument("--generation", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "start":
            result = start_daily_session()
            print(json.dumps(result, sort_keys=True))
            return 0
        if arguments.command == "status":
            result = session_status()
            print(json.dumps(result, sort_keys=True))
            return 0 if result["status"] == "active" else 2
        if arguments.command == "stop":
            removed = _remove_any(os.environ)
            print(json.dumps(_safe_result("stopped" if removed else "inactive"), sort_keys=True))
            return 0
        if arguments.command == "path":
            print(session_path())
            return 0
        if arguments.command == "_emit":
            if sys.stdout.isatty():
                raise DevSessionError("credential_tty_output_refused")
            print(emit_credential(arguments.slot), end="")
            return 0
        if arguments.command == "_expire":
            generation = arguments.generation
            if _GENERATION_RE.fullmatch(generation) is None:
                raise DevSessionError("generation_invalid")
            _remove_generation(os.environ, generation)
            return 0
    except DevSessionError as error:
        print(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "status": "error",
                    "error_code": error.code,
                    "credential_values_exposed": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
