#!/usr/bin/env python3
"""The bounded, read-only Phase 5 Trip Planner command interface.

The initial read-only commands are ``inspect`` (legacy evidence review) and
``validate`` (deterministic legacy timeline review).  Neither calls a
provider, mutates trip data, opens an EvidenceStore, migrates, renders, or
deploys.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trip_planner.tripctl import (  # noqa: E402
    TRIPCTL_VERSION,
    TripctlError,
    inspect_trip,
    inspection_failure,
    validate_trip,
    validation_failure,
)


class _ArgumentError(ValueError):
    """Prevent argparse from printing unstructured, caller-derived errors."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description="Read a legacy trip through redacted, read-only reviews.",
        allow_abbrev=False,
        add_help=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        dest="help_requested",
        action="store_true",
    )
    parser.add_argument(
        "--version",
        dest="version_requested",
        action="store_true",
    )
    commands = parser.add_subparsers(
        dest="command",
        parser_class=_Parser,
    )
    inspect = commands.add_parser(
        "inspect",
        help="emit a redacted, read-only legacy evidence review envelope",
        allow_abbrev=False,
        add_help=False,
    )
    inspect.add_argument(
        "-h",
        "--help",
        dest="inspect_help_requested",
        action="store_true",
    )
    inspect.add_argument(
        "trip_path",
        help="legacy trip directory or its data directory",
        nargs="?",
    )
    validate = commands.add_parser(
        "validate",
        help="emit a redacted, read-only legacy timeline review envelope",
        allow_abbrev=False,
        add_help=False,
    )
    validate.add_argument(
        "-h",
        "--help",
        dest="validate_help_requested",
        action="store_true",
    )
    validate.add_argument(
        "trip_path",
        help="legacy trip directory or its data directory",
        nargs="?",
    )
    return parser


def _emit(payload: dict[str, object], *, error: bool) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )


def _information(command: str, result: dict[str, object]) -> dict[str, object]:
    return {
        "contract_version": TRIPCTL_VERSION,
        "command": command,
        "ok": True,
        "status": "information",
        "storage_mode": "unknown",
        "result": result,
        "problems": [],
        "retryable": False,
        "pending_review_retained": False,
        "next_action": "none",
        "requires_user_review": False,
    }


def _invalid_argument_failure(argv: list[str]) -> dict[str, object]:
    """Select a command envelope without reflecting arbitrary caller input."""

    if argv and argv[0] == "validate":
        return validation_failure(TripctlError("INVALID_ARGUMENT"))
    return inspection_failure(TripctlError("INVALID_ARGUMENT"))


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    try:
        arguments = _parser().parse_args(raw_argv)
    except _ArgumentError:
        _emit(
            _invalid_argument_failure(raw_argv),
            error=True,
        )
        return 2

    if arguments.version_requested:
        _emit(
            _information("version", {"version": TRIPCTL_VERSION}),
            error=False,
        )
        return 0
    if arguments.help_requested:
        _emit(
            _information(
                "help",
                {
                    "available_commands": ["inspect", "validate"],
                    "read_only": True,
                },
            ),
            error=False,
        )
        return 0
    if arguments.command is None:
        _emit(
            inspection_failure(TripctlError("INVALID_ARGUMENT")),
            error=True,
        )
        return 2
    if arguments.command == "inspect":
        if arguments.inspect_help_requested:
            _emit(
                _information(
                    "inspect",
                    {"usage": "tripctl inspect <trip_path>"},
                ),
                error=False,
            )
            return 0
        if arguments.trip_path is None:
            _emit(
                inspection_failure(TripctlError("INVALID_ARGUMENT")),
                error=True,
            )
            return 2
        try:
            payload = inspect_trip(arguments.trip_path)
        except TripctlError as exc:
            _emit(inspection_failure(exc), error=True)
            return 2
        _emit(payload, error=False)
        return 0

    if arguments.command == "validate":
        if arguments.validate_help_requested:
            _emit(
                _information(
                    "validate",
                    {"usage": "tripctl validate <trip_path>"},
                ),
                error=False,
            )
            return 0
        if arguments.trip_path is None:
            _emit(
                validation_failure(TripctlError("INVALID_ARGUMENT")),
                error=True,
            )
            return 2
        try:
            payload = validate_trip(arguments.trip_path)
        except TripctlError as exc:
            _emit(validation_failure(exc), error=True)
            return 2
        _emit(payload, error=False)
        return 0

    # argparse owns the command choices, but retain a redacted fallback if a
    # future parser change introduces an unexpected namespace value.
    _emit(
        inspection_failure(TripctlError("INVALID_ARGUMENT")),
        error=True,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
