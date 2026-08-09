"""Offline tests for the private 24-hour Trip Planner dev session."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from scripts import trip_planner_dev_session as dev_session


_GOOGLE_KEY = "AIza" + "A" * 35
_SERP_KEY = "b" * 64
_BW_SESSION_SENTINEL = "private-bitwarden-session-sentinel"
_BOOT_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_BOOT_ID = "22222222-2222-2222-2222-222222222222"
_BOOTTIME_NS = 2_000_000_000


class TripPlannerDevSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name)
        self.runtime.chmod(0o700)
        self.environment = {"XDG_RUNTIME_DIR": str(self.runtime)}
        self.runtime_patch = mock.patch.object(
            dev_session,
            "_EXPECTED_RUNTIME_ROOT",
            self.runtime,
        )
        self.runtime_patch.start()

    def tearDown(self) -> None:
        self.runtime_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def _credential_loader(_session: str, slot: str) -> str | None:
        return {
            "GOOGLE_MAPS_API_KEY": _GOOGLE_KEY,
            "SERPAPI_API_KEY": _SERP_KEY,
        }.get(slot)

    def _start(
        self,
        *,
        now_epoch: int = 1_000,
        boottime_ns: int = _BOOTTIME_NS,
        boot_id: str = _BOOT_ID,
    ) -> dict[str, object]:
        return dev_session.start_daily_session(
            self.environment,
            now_epoch=now_epoch,
            boottime_ns=boottime_ns,
            boot_id=boot_id,
            acquire_session=lambda _environment: _BW_SESSION_SENTINEL,
            read_credential=self._credential_loader,
            schedule_expiry=lambda _generation, _expires: None,
        )

    def _status(
        self,
        *,
        now_epoch: int,
        boottime_ns: int,
        boot_id: str = _BOOT_ID,
    ) -> dict[str, object]:
        return dev_session.session_status(
            self.environment,
            now_epoch=now_epoch,
            boottime_ns=boottime_ns,
            boot_id=boot_id,
        )

    def test_start_caches_only_allowlisted_provider_keys_for_exactly_24_hours(
        self,
    ) -> None:
        result = self._start()

        self.assertEqual("started", result["status"])
        self.assertEqual(86_400, result["ttl_seconds"])
        self.assertEqual(
            ["GOOGLE_MAPS_API_KEY", "SERPAPI_API_KEY"],
            result["credential_slots"],
        )
        rendered = json.dumps(result, sort_keys=True)
        self.assertNotIn(_GOOGLE_KEY, rendered)
        self.assertNotIn(_SERP_KEY, rendered)
        self.assertNotIn(_BW_SESSION_SENTINEL, rendered)

        path = dev_session.session_path(self.environment)
        info = path.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(0o600, stat.S_IMODE(info.st_mode))
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(_BOOT_ID, payload["boot_id"])
        self.assertEqual(1_000, payload["created_at_epoch"])
        self.assertEqual(87_400, payload["expires_at_epoch"])
        self.assertEqual(_BOOTTIME_NS, payload["created_boottime_ns"])
        self.assertEqual(
            _BOOTTIME_NS + 86_400 * 1_000_000_000,
            payload["expires_boottime_ns"],
        )
        self.assertNotIn(_BW_SESSION_SENTINEL, path.read_text(encoding="utf-8"))

    def test_status_and_already_active_never_reacquire_or_expose_values(self) -> None:
        self._start()
        active = self._status(now_epoch=1_001, boottime_ns=_BOOTTIME_NS + 1)
        self.assertEqual("active", active["status"])

        def unexpected_acquire(_environment: object) -> str:
            raise AssertionError("active session must not unlock again")

        reused = dev_session.start_daily_session(
            self.environment,
            now_epoch=1_002,
            boottime_ns=_BOOTTIME_NS + 2,
            boot_id=_BOOT_ID,
            acquire_session=unexpected_acquire,
            read_credential=self._credential_loader,
            schedule_expiry=lambda _generation, _expires: None,
        )
        self.assertEqual("already_active", reused["status"])
        rendered = json.dumps([active, reused], sort_keys=True)
        for private_value in (_GOOGLE_KEY, _SERP_KEY, _BW_SESSION_SENTINEL):
            self.assertNotIn(private_value, rendered)

    def test_direnv_bridge_is_required_and_emits_only_requested_slot(self) -> None:
        self._start()
        with self.assertRaisesRegex(
            dev_session.DevSessionError,
            "direnv_bridge_required",
        ):
            dev_session.emit_credential(
                "GOOGLE_MAPS_API_KEY",
                self.environment,
                now_epoch=1_001,
                boottime_ns=_BOOTTIME_NS + 1,
                boot_id=_BOOT_ID,
            )

        bridge_environment = {
            **self.environment,
            "TRIP_PLANNER_DIRENV_BRIDGE": "1",
        }
        self.assertEqual(
            _GOOGLE_KEY,
            dev_session.emit_credential(
                "GOOGLE_MAPS_API_KEY",
                bridge_environment,
                now_epoch=1_001,
                boottime_ns=_BOOTTIME_NS + 1,
                boot_id=_BOOT_ID,
            ),
        )

    def test_wall_or_boottime_expiry_and_clock_rollback_remove_record(self) -> None:
        self._start()
        wall_expired = self._status(
            now_epoch=87_400,
            boottime_ns=_BOOTTIME_NS + 1,
        )
        self.assertEqual("inactive", wall_expired["status"])
        self.assertFalse(dev_session.session_path(self.environment).exists())

        self._start(now_epoch=2_000, boottime_ns=3_000_000_000)
        boot_expired = self._status(
            now_epoch=2_001,
            boottime_ns=3_000_000_000 + 86_400 * 1_000_000_000,
        )
        self.assertEqual("inactive", boot_expired["status"])

        self._start(now_epoch=3_000, boottime_ns=4_000_000_000)
        wall_rollback = self._status(
            now_epoch=2_999,
            boottime_ns=4_000_000_001,
        )
        self.assertEqual("inactive", wall_rollback["status"])

        self._start(now_epoch=4_000, boottime_ns=5_000_000_000)
        boot_rollback = self._status(
            now_epoch=4_001,
            boottime_ns=4_999_999_999,
        )
        self.assertEqual("inactive", boot_rollback["status"])

    def test_boot_change_and_wrong_generation_fail_closed(self) -> None:
        self._start()
        record = dev_session._read_record(
            self.environment,
            now_epoch=1_001,
            boottime_ns=_BOOTTIME_NS + 1,
            boot_id=_BOOT_ID,
        )
        assert record is not None
        self.assertFalse(
            dev_session._remove_generation(self.environment, "0" * 32)
        )
        self.assertTrue(dev_session.session_path(self.environment).exists())
        self.assertTrue(
            dev_session._remove_generation(self.environment, record.generation)
        )
        self.assertFalse(dev_session.session_path(self.environment).exists())

        self._start()
        changed_boot = self._status(
            now_epoch=1_002,
            boottime_ns=_BOOTTIME_NS + 2,
            boot_id=_OTHER_BOOT_ID,
        )
        self.assertEqual("inactive", changed_boot["status"])
        self.assertFalse(dev_session.session_path(self.environment).exists())

    def test_timer_failure_removes_new_credentials(self) -> None:
        def fail_timer(_generation: str, _expires: int) -> None:
            raise dev_session.DevSessionError("expiry_timer_unavailable")

        with self.assertRaisesRegex(
            dev_session.DevSessionError,
            "expiry_timer_unavailable",
        ):
            dev_session.start_daily_session(
                self.environment,
                now_epoch=1_000,
                boottime_ns=_BOOTTIME_NS,
                boot_id=_BOOT_ID,
                acquire_session=lambda _environment: _BW_SESSION_SENTINEL,
                read_credential=self._credential_loader,
                schedule_expiry=fail_timer,
            )
        self.assertFalse(dev_session.session_path(self.environment).exists())

    def test_insecure_or_unexpected_runtime_and_symlink_record_are_rejected(
        self,
    ) -> None:
        with mock.patch.object(
            dev_session,
            "_EXPECTED_RUNTIME_ROOT",
            self.runtime / "different-root",
        ):
            with self.assertRaisesRegex(
                dev_session.DevSessionError,
                "runtime_dir_unsafe",
            ):
                self._status(now_epoch=1_000, boottime_ns=_BOOTTIME_NS)

        self.runtime.chmod(0o755)
        with self.assertRaisesRegex(
            dev_session.DevSessionError,
            "runtime_dir_unsafe",
        ):
            self._status(now_epoch=1_000, boottime_ns=_BOOTTIME_NS)

        self.runtime.chmod(0o700)
        session_dir = self.runtime / "trip-planner"
        session_dir.mkdir(mode=0o700)
        dev_session.session_path(self.environment).symlink_to("/dev/null")
        with self.assertRaisesRegex(
            dev_session.DevSessionError,
            "session_record_unsafe",
        ):
            self._status(now_epoch=1_000, boottime_ns=_BOOTTIME_NS)

    def test_bitwarden_session_is_in_child_environment_not_process_arguments(
        self,
    ) -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(
            command: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            child_environment = kwargs.get("env")
            assert isinstance(child_environment, dict)
            calls.append((command, child_environment))
            return subprocess.CompletedProcess(command, 0, stdout=_GOOGLE_KEY)

        value = dev_session._read_bitwarden_credential(
            _BW_SESSION_SENTINEL,
            "GOOGLE_MAPS_API_KEY",
            run=fake_run,
        )
        self.assertEqual(_GOOGLE_KEY, value)
        self.assertEqual(
            [
                "bw",
                "get",
                "notes",
                "GOOGLE_MAPS_API_KEY",
                "--nointeraction",
            ],
            calls[0][0],
        )
        self.assertNotIn(_BW_SESSION_SENTINEL, calls[0][0])
        self.assertEqual(_BW_SESSION_SENTINEL, calls[0][1]["BW_SESSION"])

    def test_stale_inherited_bitwarden_session_gets_one_fresh_unlock(self) -> None:
        environment = {
            **self.environment,
            "BW_SESSION": _BW_SESSION_SENTINEL,
        }

        def read(session: str, slot: str) -> str | None:
            if session == _BW_SESSION_SENTINEL:
                return None
            return self._credential_loader(session, slot)

        with mock.patch.object(
            dev_session,
            "_unlock_bitwarden_session",
            return_value="fresh-private-session-sentinel",
        ) as unlock:
            result = dev_session.start_daily_session(
                environment,
                now_epoch=1_000,
                boottime_ns=_BOOTTIME_NS,
                boot_id=_BOOT_ID,
                read_credential=read,
                schedule_expiry=lambda _generation, _expires: None,
            )

        self.assertEqual("started", result["status"])
        unlock.assert_called_once_with()
        serialized = dev_session.session_path(environment).read_text(encoding="utf-8")
        self.assertNotIn(_BW_SESSION_SENTINEL, serialized)
        self.assertNotIn("fresh-private-session-sentinel", serialized)

    def test_google_key_is_required_while_serp_key_remains_optional(self) -> None:
        with self.assertRaisesRegex(
            dev_session.DevSessionError,
            "google_maps_credential_unavailable",
        ):
            dev_session.start_daily_session(
                self.environment,
                now_epoch=1_000,
                boottime_ns=_BOOTTIME_NS,
                boot_id=_BOOT_ID,
                acquire_session=lambda _environment: _BW_SESSION_SENTINEL,
                read_credential=(
                    lambda _session, slot: _SERP_KEY
                    if slot == "SERPAPI_API_KEY"
                    else None
                ),
                schedule_expiry=lambda _generation, _expires: None,
            )
        self.assertFalse(dev_session.session_path(self.environment).exists())

    def test_expiry_timer_is_absolute_generation_bound_and_secret_free(self) -> None:
        captured: list[str] = []
        captured_environment: dict[str, str] = {}

        def fake_run(
            command: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            captured.extend(command)
            environment = kwargs.get("env")
            assert isinstance(environment, dict)
            captured_environment.update(environment)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        generation = "a" * 32
        dev_session._schedule_expiry(
            generation,
            87_400,
            self.environment,
            run=fake_run,
        )
        rendered = " ".join(captured)
        self.assertIn("--on-calendar=1970-01-02 00:16:40 UTC", rendered)
        self.assertIn("--timer-property=AccuracySec=1s", rendered)
        self.assertNotIn("Persistent=true", rendered)
        self.assertNotIn("--on-active", rendered)
        self.assertIn(generation, rendered)
        self.assertNotIn(_GOOGLE_KEY, rendered)
        self.assertNotIn(_BW_SESSION_SENTINEL, rendered)
        self.assertEqual(
            {"PATH", "LANG", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"},
            set(captured_environment),
        )
        for secret_name in (
            "BW_SESSION",
            "GOOGLE_MAPS_API_KEY",
            "SERPAPI_API_KEY",
        ):
            self.assertNotIn(secret_name, captured_environment)

    def test_tracked_direnv_bridge_is_valid_and_has_no_vault_fallback(self) -> None:
        bridge = Path(__file__).parents[1] / "scripts" / "trip_planner_direnv.sh"
        completed = subprocess.run(
            ["bash", "-n", str(bridge)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        source = bridge.read_text(encoding="utf-8")
        self.assertNotIn("bw get", source)
        self.assertNotIn("load_bw_secret", source)
        self.assertNotIn("export BW_SESSION", source)
        self.assertIn("_emit GOOGLE_MAPS_API_KEY", source)

    def test_concurrent_starts_unlock_only_once(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        acquire_count = 0
        count_lock = threading.Lock()
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def acquire(_environment: dict[str, str]) -> str:
            nonlocal acquire_count
            with count_lock:
                acquire_count += 1
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("test release timed out")
            return _BW_SESSION_SENTINEL

        def run_start() -> None:
            try:
                results.append(
                    dev_session.start_daily_session(
                        self.environment,
                        now_epoch=1_000,
                        boottime_ns=_BOOTTIME_NS,
                        boot_id=_BOOT_ID,
                        acquire_session=acquire,
                        read_credential=self._credential_loader,
                        schedule_expiry=lambda _generation, _expires: None,
                    )
                )
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=run_start)
        second = threading.Thread(target=run_start)
        first.start()
        self.assertTrue(entered.wait(timeout=1))
        second.start()
        time.sleep(0.05)
        self.assertEqual(1, acquire_count)
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(
            ["already_active", "started"],
            sorted(result["status"] for result in results),
        )
        self.assertEqual(1, acquire_count)

    def test_waiting_status_evaluates_clock_only_after_acquiring_lock(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        status_clock_evaluated = threading.Event()
        starter_clock_calls = 0
        statuses: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def evaluated_clock(**_kwargs: object) -> tuple[int, int, str]:
            nonlocal starter_clock_calls
            if threading.current_thread().name == "session-starter":
                starter_clock_calls += 1
                if starter_clock_calls == 1:
                    return 1_000, _BOOTTIME_NS, _BOOT_ID
                return 2_000, _BOOTTIME_NS + 100, _BOOT_ID
            status_clock_evaluated.set()
            return 2_001, _BOOTTIME_NS + 101, _BOOT_ID

        def acquire(_environment: dict[str, str]) -> str:
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("test release timed out")
            return _BW_SESSION_SENTINEL

        def start() -> None:
            try:
                self.assertEqual(
                    "started",
                    dev_session.start_daily_session(
                        self.environment,
                        acquire_session=acquire,
                        read_credential=self._credential_loader,
                        schedule_expiry=lambda _generation, _expires: None,
                    )["status"],
                )
            except BaseException as error:
                errors.append(error)

        def status() -> None:
            try:
                statuses.append(dev_session.session_status(self.environment))
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(
            dev_session,
            "_evaluated_clock",
            side_effect=evaluated_clock,
        ):
            starter = threading.Thread(target=start, name="session-starter")
            waiter = threading.Thread(target=status, name="status-waiter")
            starter.start()
            self.assertTrue(entered.wait(timeout=1))
            waiter.start()
            time.sleep(0.05)
            self.assertFalse(status_clock_evaluated.is_set())
            release.set()
            starter.join(timeout=2)
            waiter.join(timeout=2)

        self.assertEqual([], errors)
        self.assertEqual(2, starter_clock_calls)
        self.assertTrue(status_clock_evaluated.is_set())
        self.assertEqual("active", statuses[0]["status"])

    def test_old_expiry_cannot_delete_a_concurrent_new_generation(self) -> None:
        self._start()
        old = dev_session._read_record(
            self.environment,
            now_epoch=1_001,
            boottime_ns=_BOOTTIME_NS + 1,
            boot_id=_BOOT_ID,
        )
        assert old is not None
        entered = threading.Event()
        release = threading.Event()
        removal_results: list[bool] = []
        start_results: list[dict[str, object]] = []

        def delayed_remove(
            environ: dict[str, str],
            generation: str,
        ) -> bool:
            record = dev_session._read_record_unlocked(
                environ,
                now_epoch=0,
                boottime_ns=0,
                boot_id="00000000-0000-0000-0000-000000000000",
                remove_expired=False,
                enforce_lifetime=False,
            )
            if record is None or record.generation != generation:
                return False
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("test release timed out")
            dev_session.session_path(environ).unlink()
            return True

        with mock.patch.object(
            dev_session,
            "_remove_generation_unlocked",
            side_effect=delayed_remove,
        ):
            remover = threading.Thread(
                target=lambda: removal_results.append(
                    dev_session._remove_generation(
                        self.environment,
                        old.generation,
                    )
                )
            )
            starter = threading.Thread(
                target=lambda: start_results.append(
                    self._start(now_epoch=2_000, boottime_ns=3_000_000_000)
                )
            )
            remover.start()
            self.assertTrue(entered.wait(timeout=1))
            starter.start()
            time.sleep(0.05)
            self.assertTrue(starter.is_alive())
            release.set()
            remover.join(timeout=2)
            starter.join(timeout=2)

        self.assertEqual([True], removal_results)
        self.assertEqual("started", start_results[0]["status"])
        current = dev_session._read_record(
            self.environment,
            now_epoch=2_001,
            boottime_ns=3_000_000_001,
            boot_id=_BOOT_ID,
        )
        assert current is not None
        self.assertNotEqual(old.generation, current.generation)

    def test_internal_emit_refuses_a_tty(self) -> None:
        class TtyBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        with redirect_stdout(TtyBuffer()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                2,
                dev_session.main(["_emit", "GOOGLE_MAPS_API_KEY"]),
            )


if __name__ == "__main__":
    unittest.main()
