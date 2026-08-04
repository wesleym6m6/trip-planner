#!/usr/bin/env python3
"""Print a redacted, read-only Phase 4.6B legacy evidence preview.

This command has no migrate, import, provider, render, deployment, or cleanup
option.  A successful output is a review artifact only and never authorizes a
destructive operation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trip_planner.legacy_evidence import (  # noqa: E402
    LEGACY_EVIDENCE_PREVIEW_VERSION,
    LegacyEvidencePreviewError,
    preview_legacy_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read legacy evidence metadata without importing, migrating, or "
            "cleaning any trip data."
        )
    )
    parser.add_argument(
        "trip_path",
        help="legacy trip directory or its data directory",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=LEGACY_EVIDENCE_PREVIEW_VERSION,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        preview = preview_legacy_evidence(arguments.trip_path)
    except LegacyEvidencePreviewError as exc:
        print(
            json.dumps(
                {
                    "contract_version": LEGACY_EVIDENCE_PREVIEW_VERSION,
                    "error": {"code": exc.code},
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (MemoryError, OverflowError, RecursionError):
        print(
            json.dumps(
                {
                    "contract_version": LEGACY_EVIDENCE_PREVIEW_VERSION,
                    "error": {"code": "LEGACY_EVIDENCE_SOURCE_UNSAFE"},
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(preview.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
