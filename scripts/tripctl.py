#!/usr/bin/env python3
"""The bounded, read-only Phase 5 Trip Planner command interface.

``inspect`` and ``validate`` dispatch to legacy or canonical storage;
canonical-only ``propose`` and ``score`` use opaque deterministic replay refs.
All keep one redacted JSON envelope.  None calls a provider, mutates trip data,
opens an EvidenceStore, migrates, renders, or deploys.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trip_planner.tripctl import (  # noqa: E402
    TRIPCTL_VERSION,
    TripctlError,
    inspect_trip,
    inspection_failure,
    proposal_failure,
    propose_trip,
    score_failure,
    score_trip,
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
        description="Read a trip through redacted, read-only reviews.",
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
        help="emit a redacted, read-only storage inspection envelope",
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
        help="trip directory or its data directory",
        nargs="?",
    )
    propose = commands.add_parser(
        "propose",
        help="emit one opaque deterministic canonical schedule proposal",
        allow_abbrev=False,
        add_help=False,
    )
    propose.add_argument(
        "-h",
        "--help",
        dest="propose_help_requested",
        action="store_true",
    )
    propose.add_argument(
        "trip_path",
        help="canonical trip directory or its data directory",
        nargs="?",
    )
    propose.add_argument(
        "--evaluation-at",
        dest="evaluation_at",
        help="caller-pinned timezone-aware ISO-8601 evaluation instant",
    )
    score = commands.add_parser(
        "score",
        help="trusted-replay and score one opaque canonical proposal",
        allow_abbrev=False,
        add_help=False,
    )
    score.add_argument(
        "-h",
        "--help",
        dest="score_help_requested",
        action="store_true",
    )
    score.add_argument(
        "trip_path",
        help="canonical trip directory or its data directory",
        nargs="?",
    )
    score.add_argument(
        "--evaluation-at",
        dest="evaluation_at",
        help="same caller-pinned timezone-aware ISO-8601 proposal instant",
    )
    score.add_argument(
        "--proposal-ref",
        dest="proposal_ref",
        help="opaque proposal ref returned by tripctl propose",
    )
    validate = commands.add_parser(
        "validate",
        help="emit a redacted, read-only timeline review envelope",
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
        help="trip directory or its data directory",
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
    if argv and argv[0] == "propose":
        return proposal_failure(TripctlError("INVALID_ARGUMENT"))
    if argv and argv[0] == "score":
        return score_failure(TripctlError("INVALID_ARGUMENT"))
    return inspection_failure(TripctlError("INVALID_ARGUMENT"))


def _evaluation_at(value: str | None) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TripctlError("INVALID_EVALUATION_AT")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise TripctlError("INVALID_EVALUATION_AT") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TripctlError("INVALID_EVALUATION_AT")
    return parsed


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
                    "available_commands": [
                        "inspect",
                        "propose",
                        "score",
                        "validate",
                    ],
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

    if arguments.command == "propose":
        if arguments.propose_help_requested:
            _emit(
                _information(
                    "propose",
                    {
                        "usage": (
                            "tripctl propose <trip_path> "
                            "--evaluation-at <ISO-8601>"
                        )
                    },
                ),
                error=False,
            )
            return 0
        if arguments.trip_path is None:
            _emit(
                proposal_failure(TripctlError("INVALID_ARGUMENT")),
                error=True,
            )
            return 2
        try:
            evaluation_at = _evaluation_at(arguments.evaluation_at)
            payload = propose_trip(
                arguments.trip_path,
                evaluation_at=evaluation_at,
            )
        except TripctlError as exc:
            _emit(proposal_failure(exc), error=True)
            return 2
        _emit(payload, error=False)
        return 0

    if arguments.command == "score":
        if arguments.score_help_requested:
            _emit(
                _information(
                    "score",
                    {
                        "usage": (
                            "tripctl score <trip_path> --proposal-ref <ref> "
                            "--evaluation-at <ISO-8601>"
                        )
                    },
                ),
                error=False,
            )
            return 0
        if arguments.trip_path is None or arguments.proposal_ref is None:
            _emit(
                score_failure(TripctlError("INVALID_ARGUMENT")),
                error=True,
            )
            return 2
        try:
            evaluation_at = _evaluation_at(arguments.evaluation_at)
            payload = score_trip(
                arguments.trip_path,
                proposal_ref=arguments.proposal_ref,
                evaluation_at=evaluation_at,
            )
        except TripctlError as exc:
            _emit(score_failure(exc), error=True)
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
