#!/usr/bin/env python3
"""Build a verified public-only Trip Planner artifact tree."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trip_planner.public_release import PublicReleaseError, build_public_site


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an explicit, verified public Trip Planner release."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = build_public_site(
            REPO_ROOT / "public" / "release.json",
            arguments.output_dir,
            source_root=REPO_ROOT / "public",
            template_dir=REPO_ROOT / "template",
        )
    except PublicReleaseError as error:
        print(
            json.dumps(
                {"ok": False, "status": "rejected", "code": error.code},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"ok": True, "status": "built", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
